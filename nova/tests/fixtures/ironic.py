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

import copy
from unittest import mock

import fixtures
from openstack.baremetal.v1 import _proxy as baremetal_proxy
from openstack import exceptions as sdk_exc

from nova.tests.unit.virt.ironic import utils as ironic_utils
from nova.virt.ironic import driver as ironic_driver
from nova.virt.ironic import ironic_states


class IronicFixture(fixtures.Fixture):
    """A fake Ironic API for the Ironic virt driver.

    This replaces IronicDriver.ironic_connection with an autospec of the
    openstacksdk baremetal proxy, backed by an in-memory set of nodes, so that
    functional tests can run the real Ironic driver. Like the real API, it
    returns copies of the nodes, so changes made by a test (standing in for
    Ironic) are only seen by Nova when it asks Ironic again.

    IronicDriver.spawn is replaced too, by default with what a successful
    Ironic deploy does to the node. Tests can change the behaviour of the
    fake API or of spawn by setting side_effect on ``connection`` or ``spawn``.
    """

    def __init__(self, test):
        super().__init__()
        self.test = test
        self.nodes = {}

    def setUp(self):
        super().setUp()
        self.connection = mock.create_autospec(
            baremetal_proxy.Proxy, instance=True)
        self.connection.nodes.side_effect = self._nodes
        self.connection.get_node.side_effect = self._get_node
        self.connection.set_node_provision_state.side_effect = (
            self._set_node_provision_state)
        self.useFixture(fixtures.MockPatchObject(
            ironic_driver.IronicDriver, 'ironic_connection',
            new_callable=mock.PropertyMock, return_value=self.connection))
        self.spawn = self.useFixture(fixtures.MockPatchObject(
            ironic_driver.IronicDriver, 'spawn', autospec=True,
            side_effect=self._spawn)).mock
        # Don't wait between polls of the node.
        self.test.flags(api_retry_interval=0, group='ironic')

    def add_node(self, **kwargs):
        """Add a node to the fake Ironic and return it.

        The returned node is the one stored in the fake: tests change it to
        change what Ironic reports.
        """
        node = ironic_utils.get_test_node(**kwargs)
        self.nodes[node.id] = node
        return node

    def _find_node(self, node_id):
        if node_id is None:
            # This is what the SDK raises before sending the request.
            raise sdk_exc.InvalidRequest(
                'Request requires an ID but none was found')
        try:
            return self.nodes[node_id]
        except KeyError:
            raise sdk_exc.ResourceNotFound(
                'Node %s could not be found.' % node_id)

    def _get_node(self, node_id, fields=None):
        return copy.deepcopy(self._find_node(node_id))

    def _nodes(self, details=False, instance_id=None, associated=None,
               **query):
        nodes = self.nodes.values()
        if instance_id is not None:
            nodes = [n for n in nodes if n.instance_id == instance_id]
        if associated is not None:
            nodes = [n for n in nodes
                     if (n.instance_id is not None) == associated]
        return iter([copy.deepcopy(n) for n in nodes])

    def _set_node_provision_state(self, node_id, target, **kwargs):
        node = self._find_node(node_id)
        if target == 'deleted':
            # Tear-down and cleaning are done.
            node.instance_id = None
            node.provision_state = ironic_states.AVAILABLE
            node.power_state = ironic_states.POWER_OFF
        return copy.deepcopy(node)

    def _spawn(self, context, instance, *args, **kwargs):
        node = self._find_node(instance.node)
        node.instance_id = instance.uuid
        node.provision_state = ironic_states.ACTIVE
        node.power_state = ironic_states.POWER_ON
