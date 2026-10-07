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

from unittest import mock

import fixtures
from openstack import exceptions as sdk_exc
from oslo_utils.fixture import uuidsentinel as uuids

from nova import context
from nova import exception
from nova.network import neutron
from nova import test
from nova.tests import fixtures as nova_fixtures
from nova.tests.functional import fixtures as func_fixtures
from nova.tests.functional import integrated_helpers
from nova.virt.ironic import driver as ironic_driver
from nova.virt.ironic import ironic_states


class LongNodeLockIronicFixture(nova_fixtures.IronicFixture):
    """A fake Ironic API in which the conductor holds the node lock.

    While the node is deploying, the conductor holds its lock, and Ironic
    rejects provision state requests with 409 Conflict. The node stays
    deploying for longer than Nova waits, with or without a fix for bug
    2169776.
    """

    def _set_node_provision_state(self, node_id, target, **kwargs):
        if self._find_node(node_id).provision_state == (
                ironic_states.DEPLOYING):
            raise sdk_exc.ConflictException(
                'Node %s is locked by host conductor, please retry after the '
                'current operation is completed.' % node_id)
        return super()._set_node_provision_state(node_id, target, **kwargs)


class BuildAbortCleanupIronicNodeLeakTestCase(
    test.TestCase, integrated_helpers.PlacementInstanceHelperMixin,
):
    """Regression test for bug 2169779.

    When the cleanup of a failed build fails, _build_resources raises
    BuildAbortException. Nova then removes the instance from the host,
    although the driver has just failed to unprovision the node: aborting the
    resource claim clears instance.host and node, and the BuildAbortException
    handler in _do_build_and_run_instance deletes the instance's ports and
    its allocation, and clears host and node again. If the user deleted the
    server during the build, terminate_instance runs next, and when its
    driver.destroy fails, _shutdown_instance deallocates the network again.
    With instance.host None, every later delete of the instance is a local
    delete that never calls the Ironic driver, so the node is left
    provisioned with the deleted instance.

    This runs the real Ironic driver against a fake Ironic API with one node,
    see LongNodeLockIronicFixture. IronicDriver.spawn is replaced by starting
    the deploy and raising.
    """

    def setUp(self):
        super().setUp()
        self.useFixture(nova_fixtures.RealPolicyFixture())
        self.neutron = self.useFixture(nova_fixtures.NeutronFixture(self))
        self.useFixture(nova_fixtures.GlanceFixture(self))
        self.placement = self.useFixture(func_fixtures.PlacementFixture()).api
        self.api_fixture = self.useFixture(nova_fixtures.OSAPIFixture(
            api_version='v2.1'))
        self.api = self.api_fixture.admin_api
        self.api.microversion = 'latest'

        self.ironic = self.useFixture(LongNodeLockIronicFixture(self))
        self.node = self.ironic.add_node(
            id=uuids.node, resource_class='baremetal',
            provision_state=ironic_states.AVAILABLE,
            power_state=ironic_states.POWER_OFF,
            properties={'cpus': 1, 'memory_mb': 1024, 'local_gb': 10,
                        'cpu_arch': 'x86_64'})
        # The Ironic driver leaves the ports unbound for Ironic to bind
        # later, which NeutronFixture.update_port does not support, so bind
        # them to the compute host here. In production they stay unbound.
        self.useFixture(fixtures.MockPatchObject(
            ironic_driver.IronicDriver, 'network_binding_host_id',
            return_value='compute'))
        # Record each time Nova deletes the instance's ports.
        self.deallocations = []
        deallocate_for_instance = neutron.API.deallocate_for_instance

        def _deallocate_for_instance(api, context, instance, **kwargs):
            self.deallocations.append(instance.uuid)
            return deallocate_for_instance(api, context, instance, **kwargs)

        self.useFixture(fixtures.MonkeyPatch(
            'nova.network.neutron.API.deallocate_for_instance',
            _deallocate_for_instance))

        # Give up waiting quickly if bug 2169776 is fixed.
        self.flags(api_max_retries=1, group='ironic')
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

    def _start_deploy(self, instance, network_info):
        # Ironic attaches these ports to the node during the deploy.
        self.port_ids = [vif['id'] for vif in network_info]
        self.node.instance_id = instance.uuid
        self.node.provision_state = ironic_states.DEPLOYING
        self.node.power_state = ironic_states.POWER_ON

    def _spawn_api_error(self, context, instance, *args, **kwargs):
        self._start_deploy(instance, kwargs['network_info'])
        # Nova gets an error from Ironic while it waits for the deploy.
        raise sdk_exc.HttpException('Service Unavailable', http_status=503)

    def _spawn_delete(self, context, instance, *args, **kwargs):
        self._start_deploy(instance, kwargs['network_info'])
        # The user deletes the server while the node is deploying. The
        # terminate waits for the build to release the instance lock.
        self.api.delete_server(instance.uuid)
        raise exception.InstanceDeployFailure(
            'Instance %s provisioning was aborted' % instance.uuid)

    def _create_server_on_network(self, **kwargs):
        return self._create_server(
            flavor_id=self.flavor_id,
            networks=[{'uuid': nova_fixtures.NeutronFixture.network_1['id']}],
            **kwargs)

    def _port_ids(self):
        return [port['id'] for port in self.neutron.list_ports(
            is_admin=True)['ports']]

    def _assert_orphan_compute_node(self):
        log = self.stdlog.logger.output
        self.assertIn('hypervisor host is %s' % uuids.node, log)
        self.assertIn('We are not deleting this', log)

    def test_build_abort_cleanup_node_is_leaked(self):
        self.ironic.spawn.side_effect = self._spawn_api_error
        server = self._create_server_on_network(expected_state='ERROR')

        log = self.stdlog.logger.output
        self.assertIn(
            mock.call(uuids.node, 'deleted'),
            self.ironic.connection.set_node_provision_state.call_args_list)
        self.assertIn(
            'Could not clean up failed build, not rescheduling', log)
        # Aborting the claim cleared instance.node before the network cleanup,
        # so unplug_vifs() asked Ironic for node None.
        self.assertIn(
            'Cleaning up VIFs failed for instance. Error: Request requires an '
            'ID but none was found', log)

        # FIXME(shermanm): This is bug 2169779. The build was aborted because
        # the build cleanup could not unprovision the node, but Nova removed
        # the instance from the host anyway while the node was still
        # deploying: it cleared instance.host and deleted the instance's
        # ports and allocation. Only the BuildAbortException handler ran, so
        # it deleted the ports once.
        self.assertIsNone(server['OS-EXT-SRV-ATTR:host'])
        self.assertEqual([server['id']], self.deallocations)
        self.assertEqual(1, len(self.port_ids))
        self.assertNotIn(self.port_ids[0], self._port_ids())
        self.assertEqual(
            {}, self._get_allocations_by_server_uuid(server['id']))
        self.assertEqual(ironic_states.DEPLOYING, self.node.provision_state)
        self.assertEqual(server['id'], self.node.instance_id)

        # With instance.host None, node_cache leaves out the node, so Nova
        # reports the node's compute node as an orphan while the server is in
        # ERROR.
        self.compute.manager.update_available_resource(
            context.get_admin_context())
        self._assert_orphan_compute_node()

        # Ironic's deploy then fails at switch_to_tenant_network because the
        # ports are gone, and the node goes to deploy failed.
        self.node.provision_state = ironic_states.DEPLOYFAIL
        requests = self.ironic.connection.set_node_provision_state.call_count

        # FIXME(shermanm): This is bug 2169779. Deleting the ERROR server
        # should reach nova-compute so that the driver unprovisions the node.
        # Instead instance.host is None, so nova-api does a local delete that
        # never calls the driver, and the node stays in deploy failed with the
        # deleted instance.
        self._delete_server(server)
        self.assertIn(
            "instance's host None is down, deleting from database",
            self.stdlog.logger.output)
        self.assertEqual(
            requests,
            self.ironic.connection.set_node_provision_state.call_count)
        self.assertEqual(ironic_states.DEPLOYFAIL, self.node.provision_state)
        self.assertEqual(server['id'], self.node.instance_id)

    def test_build_abort_cleanup_and_terminate_delete_ports(self):
        self.ironic.spawn.side_effect = self._spawn_delete
        server = self._create_server_on_network(expected_state='BUILD')
        self._wait_for_instance_action_event(
            server, 'delete', 'compute_terminate_instance', 'Error')
        self.assertIn(
            'Could not clean up failed build, not rescheduling',
            self.stdlog.logger.output)

        # FIXME(shermanm): This is bug 2169779. Neither the build cleanup nor
        # terminate_instance could unprovision the node, but both deallocated
        # the instance's network: the BuildAbortException handler, and then
        # terminate_instance's _shutdown_instance after driver.destroy failed.
        # instance.host is cleared, and the node stays deploying with the
        # instance.
        self.assertEqual([server['id']] * 2, self.deallocations)
        self.assertNotIn(self.port_ids[0], self._port_ids())
        server = self.api.get_server(server['id'])
        self.assertEqual('ERROR', server['status'])
        self.assertIsNone(server['OS-EXT-SRV-ATTR:host'])
        self.assertEqual(ironic_states.DEPLOYING, self.node.provision_state)
        self.assertEqual(server['id'], self.node.instance_id)
