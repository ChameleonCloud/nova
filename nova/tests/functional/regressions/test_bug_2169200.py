# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from oslo_utils.fixture import uuidsentinel as uuids

from nova.compute import vm_states
from nova import context
from nova import objects
from nova import test
from nova.tests import fixtures as nova_fixtures
from nova.tests.functional import fixtures as func_fixtures
from nova.tests.functional import integrated_helpers
from nova.virt.ironic import ironic_states


class LocalDeleteIronicNodeLeakTestCase(
    test.TestCase, integrated_helpers.InstanceHelperMixin,
):
    """Regression test for bug 2169200.

    The bug was introduced in 2024.1 (Caracal) by commit fa3cf7d50c
    ("[ironic] Partition & use cache for list_instance*"), which was
    backported to 2023.2, 2023.1 and zed.

    When nova-api deletes an instance locally, nothing calls the virt driver,
    so the node stays provisioned with the deleted instance. nova-api does a
    local delete when the instance's nova-compute service is down, and when
    instance.host is None, which is the case after a failed build cleanup
    (bug 2169779). ComputeManager._cleanup_running_deleted_instances is meant
    to unprovision such nodes. It finds them through
    IronicDriver.list_instance_uuids(), which since that change reads
    node_cache, and node_cache leaves out nodes whose instance is not one of
    this host's instances.

    This runs the real Ironic driver against a fake Ironic API with one node.
    """

    def setUp(self):
        super().setUp()
        self.useFixture(nova_fixtures.RealPolicyFixture())
        self.useFixture(nova_fixtures.NeutronFixture(self))
        self.useFixture(nova_fixtures.GlanceFixture(self))
        self.useFixture(func_fixtures.PlacementFixture())
        self.api_fixture = self.useFixture(nova_fixtures.OSAPIFixture(
            api_version='v2.1'))
        self.api = self.api_fixture.admin_api
        # The latest microversion is needed to force down the compute service.
        self.api.microversion = 'latest'

        self.ironic = self.useFixture(nova_fixtures.IronicFixture(self))
        self.node = self.ironic.add_node(
            id=uuids.node, resource_class='baremetal',
            provision_state=ironic_states.AVAILABLE,
            power_state=ironic_states.POWER_OFF,
            properties={'cpus': 1, 'memory_mb': 1024, 'local_gb': 10,
                        'cpu_arch': 'x86_64'})

        # These are the defaults, set here because the tests rely on them.
        self.flags(running_deleted_instance_action='reap',
                   running_deleted_instance_timeout=0)
        # The Ironic driver uses CONF.host as the compute service host.
        self.flags(host='compute')
        self.flags(compute_driver='ironic.IronicDriver')
        self.start_service('conductor')
        self.start_service('scheduler')
        self.compute = self.start_service('compute', host='compute')

        self.flavor_id = self._create_flavor(extra_spec={
            'resources:CUSTOM_BAREMETAL': '1',
            'resources:VCPU': '0',
            'resources:MEMORY_MB': '0',
            'resources:DISK_GB': '0',
        })

    def _assert_orphan_compute_node(self):
        log = self.stdlog.logger.output
        self.assertIn('Found orphan compute node', log)
        self.assertIn('hypervisor host is %s' % uuids.node, log)
        self.assertIn('We are not deleting this', log)

    def test_local_delete_compute_down_node_is_not_unprovisioned(self):
        server = self._create_server(
            flavor_id=self.flavor_id, networks='none')
        self.assertEqual(server['id'], self.node.instance_id)

        # Delete the server while nova-compute is down: nova-api does a local
        # delete and does not contact Ironic.
        service = self.api.get_services(
            host='compute', binary='nova-compute')[0]
        self.api.put_service(service['id'], {'forced_down': True})
        self._delete_server(server)
        self.assertIn(
            "instance's host compute is down, deleting from database",
            self.stdlog.logger.output)

        # Restart nova-compute, which rebuilds node_cache, then run the
        # periodic task that should unprovision the node.
        self.api.put_service(service['id'], {'forced_down': False})
        self.compute = self.restart_compute_service(
            self.compute, keep_hypervisor_state=False)
        self.compute.manager._cleanup_running_deleted_instances(
            context.get_admin_context())

        # FIXME(shermanm): This is bug 2169200. The node should have been
        # unprovisioned, but node_cache leaves it out because its instance is
        # deleted, so _cleanup_running_deleted_instances never finds it. The
        # node stays active with the deleted instance, and nova reports it as
        # an orphan compute node.
        self.ironic.connection.set_node_provision_state.assert_not_called()
        self.assertEqual(ironic_states.ACTIVE, self.node.provision_state)
        self.assertEqual(server['id'], self.node.instance_id)
        self._assert_orphan_compute_node()

    def test_local_delete_host_none_node_is_not_unprovisioned(self):
        server = self._create_server(
            flavor_id=self.flavor_id, networks='none')
        self.assertEqual(server['id'], self.node.instance_id)

        # Leave the server and the node as Nova leaves them after a failed
        # build cleanup (bug 2169779): instance.host and node are cleared and
        # the instance is in ERROR, while the node stays provisioned with the
        # instance and the deploy later fails.
        ctxt = context.get_admin_context()
        instance = objects.Instance.get_by_uuid(ctxt, server['id'])
        instance.host = None
        instance.node = None
        instance.compute_id = None
        instance.vm_state = vm_states.ERROR
        instance.save()
        self.node.provision_state = ironic_states.DEPLOYFAIL

        # With instance.host None, nova-api does a local delete and does not
        # contact Ironic.
        self._delete_server(server)
        self.assertIn(
            "instance's host None is down, deleting from database",
            self.stdlog.logger.output)

        # Refresh node_cache, as the resource tracker periodic task does, then
        # run the periodic task that should unprovision the node.
        self.compute.manager.update_available_resource(ctxt)
        self.compute.manager._cleanup_running_deleted_instances(ctxt)

        # FIXME(shermanm): This is bug 2169200. The node should have been
        # unprovisioned, but node_cache leaves it out because its instance is
        # not one of this host's instances, so
        # _cleanup_running_deleted_instances never finds it. A fix that only
        # looks for deleted instances whose host is this host would miss it
        # too. The node stays in deploy failed with the deleted instance, and
        # nova reports it as an orphan compute node.
        self.ironic.connection.set_node_provision_state.assert_not_called()
        self.assertEqual(ironic_states.DEPLOYFAIL, self.node.provision_state)
        self.assertEqual(server['id'], self.node.instance_id)
        self._assert_orphan_compute_node()
