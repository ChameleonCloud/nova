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

from nova.conductor import api as conductor_api
from nova import exception
from nova import test
from nova.tests import fixtures as nova_fixtures
from nova.tests.functional import fixtures as func_fixtures
from nova.tests.functional import integrated_helpers
from nova.virt.ironic import ironic_states


class NodeLockIronicFixture(nova_fixtures.IronicFixture):
    """A fake Ironic API in which the conductor holds the node lock.

    While the node is deploying, the conductor holds its lock, and Ironic
    rejects provision state requests with 409 Conflict. The node goes to
    "wait call-back" and the lock is released once Nova has made
    LOCKED_REQUESTS provision state requests or read the node LOCKED_READS
    times since the deploy started. The lock outlasts what
    Nova does today (one read and one request for each of the build cleanup
    and the terminate), but a fix that retries the request and a fix that
    waits for the node both get past it.
    """

    LOCKED_REQUESTS = 2
    LOCKED_READS = 10

    def setUp(self):
        super().setUp()
        self.requests = 0
        self.reads = 0

    def _read(self, node_id):
        node = self.nodes.get(node_id)
        if node is not None and node.provision_state == (
                ironic_states.DEPLOYING):
            self.reads += 1
            if self.reads >= self.LOCKED_READS:
                node.provision_state = ironic_states.DEPLOYWAIT

    def _get_node(self, node_id, fields=None):
        self._read(node_id)
        return super()._get_node(node_id, fields=fields)

    def _nodes(self, details=False, instance_id=None, **query):
        if instance_id is not None:
            for node in list(self.nodes.values()):
                if node.instance_id == instance_id:
                    self._read(node.id)
        return super()._nodes(details=details, instance_id=instance_id,
                              **query)

    def _set_node_provision_state(self, node_id, target, **kwargs):
        node = self._find_node(node_id)
        if node.provision_state == ironic_states.DEPLOYING:
            self.requests += 1
            if self.requests <= self.LOCKED_REQUESTS:
                raise sdk_exc.ConflictException(
                    'Node %s is locked by host conductor, please retry after '
                    'the current operation is completed.' % node_id)
            node.provision_state = ironic_states.DEPLOYWAIT
        return super()._set_node_provision_state(node_id, target, **kwargs)


class DeleteDuringDeployNodeLockTestCase(
    test.TestCase, integrated_helpers.InstanceHelperMixin,
):
    """Regression test for bug 2169776.

    The bug was introduced in 2024.1 (Caracal) by commit ba44ac9baf ("Use SDK
    for node.set_provision_state"). Until then python-ironicclient retried
    409 Conflict, using [ironic]api_max_retries and api_retry_interval.

    When an instance is deleted while its node is deploying, the conductor
    holds the node lock and rejects IronicDriver._unprovision()'s request to
    set the provision state to 'deleted' with 409 Conflict. Nova does not
    retry it, so both the build cleanup and the terminate_instance for the
    delete fail, and the node stays provisioned with the instance.

    This runs the real Ironic driver against a fake Ironic API with one node,
    see NodeLockIronicFixture. IronicDriver.spawn is replaced by starting the
    deploy, deleting the server, and raising what _wait_for_active raises once
    it sees the delete. The fake replaces the openstacksdk proxy, so this test
    cannot show retries done by keystoneauth ([ironic]status_code_retries).
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
        self.api.microversion = 'latest'

        # Record reschedules, which nova-compute asks the conductor for with
        # build_instances.
        self.reschedules = []
        build_instances = conductor_api.ComputeTaskAPI.build_instances

        def _build_instances(api, context, instances, *args, **kwargs):
            self.reschedules.extend(instance.uuid for instance in instances)
            return build_instances(api, context, instances, *args, **kwargs)

        self.useFixture(fixtures.MonkeyPatch(
            'nova.conductor.api.ComputeTaskAPI.build_instances',
            _build_instances))

        self.ironic = self.useFixture(NodeLockIronicFixture(self))
        self.ironic.spawn.side_effect = self._spawn
        self.node = self.ironic.add_node(
            id=uuids.node, resource_class='baremetal',
            provision_state=ironic_states.AVAILABLE,
            power_state=ironic_states.POWER_OFF,
            properties={'cpus': 1, 'memory_mb': 1024, 'local_gb': 10,
                        'cpu_arch': 'x86_64'})

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

    def _spawn(self, context, instance, *args, **kwargs):
        self.node.instance_id = instance.uuid
        self.node.provision_state = ironic_states.DEPLOYING
        self.node.power_state = ironic_states.POWER_ON
        # The user deletes the server while the node is deploying. The
        # terminate waits for the build to release the instance lock.
        self.api.delete_server(instance.uuid)
        raise exception.InstanceDeployFailure(
            'Instance %s provisioning was aborted' % instance.uuid)

    def test_delete_during_deploy_node_is_not_unprovisioned(self):
        server = self._create_server(
            flavor_id=self.flavor_id, networks='none', expected_state='BUILD')

        # FIXME(shermanm): This is bug 2169776. Nova should wait until Ironic
        # accepts the request to unprovision the node, then unprovision it and
        # delete the server. Instead the build cleanup and the terminate each
        # ask Ironic once and give up on the 409: the build is aborted, the
        # terminate sets the server to ERROR, and the node stays deploying
        # with the instance.
        self._wait_for_instance_action_event(
            server, 'delete', 'compute_terminate_instance', 'Error')
        self.assertEqual(
            [mock.call(uuids.node, 'deleted')] * 2,
            self.ironic.connection.set_node_provision_state.call_args_list)
        log = self.stdlog.logger.output
        self.assertIn(
            'Could not clean up failed build, not rescheduling', log)
        self.assertIn('Setting instance vm_state to ERROR', log)
        server = self.api.get_server(server['id'])
        self.assertEqual('ERROR', server['status'])
        self.assertEqual(ironic_states.DEPLOYING, self.node.provision_state)
        self.assertEqual(server['id'], self.node.instance_id)

        # The build must not be rescheduled, because the user deleted the
        # server. Today the failed cleanup aborts the build. A fix that lets
        # the cleanup succeed must not turn this into a reschedule.
        self.assertEqual([], self.reschedules)
