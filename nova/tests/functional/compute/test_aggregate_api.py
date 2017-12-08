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

from nova.compute import api as compute_api
from nova import context
from nova import exception
from nova import objects
from nova import test
from nova.tests import fixtures as nova_fixtures
from nova.tests import uuidsentinel as uuids


class ComputeAggregateAPIMultiCellTestCase(test.NoDBTestCase):
    """Tests for the AggregateAPI with multiple cells allowing either service
    hosts or compute nodes to be associated with an aggregate.
    """

    USES_DB_SELF = True

    def setUp(self):
        super(ComputeAggregateAPIMultiCellTestCase, self).setUp()
        self.agg_api = compute_api.AggregateAPI()
        self.useFixture(nova_fixtures.Database(database='api'))
        celldbs = nova_fixtures.CellDatabases()
        celldbs.add_cell_database(objects.CellMapping.CELL0_UUID)
        celldbs.add_cell_database(uuids.cell1, default=True)
        celldbs.add_cell_database(uuids.cell2)
        self.useFixture(celldbs)

        self.ctxt = context.get_admin_context()
        cell0 = objects.CellMapping(
            context=self.ctxt, uuid=objects.CellMapping.CELL0_UUID,
            database_connection=objects.CellMapping.CELL0_UUID,
            transport_url='none:///')
        cell0.create()
        cell1 = objects.CellMapping(
            context=self.ctxt, uuid=uuids.cell1,
            database_connection=uuids.cell1, transport_url='none:///')
        cell1.create()
        cell2 = objects.CellMapping(
            context=self.ctxt, uuid=uuids.cell2,
            database_connection=uuids.cell2, transport_url='none:///')
        cell2.create()
        self.cell_mappings = (cell0, cell1, cell2)

        # create two Ironic nodes managed by a single nova-compute service host
        # in each of the non-cell0 cells
        for cell_id, cell in enumerate(self.cell_mappings[1:]):
            with context.target_cell(self.ctxt, cell) as cctxt:
                hostname = 'ironic_host_cell%s' % (cell_id + 1)
                svc = objects.Service(cctxt, host=hostname,
                                      binary='nova-compute',
                                      topic='nova-compute')
                svc.create()
                for node_id in (1, 2):
                    nodename = 'ironic_node_cell%s_%s' % (cell_id + 1, node_id)
                    compute_node_uuid = getattr(uuids, nodename)
                    node = objects.ComputeNode(
                        cctxt, uuid=compute_node_uuid, host=hostname,
                        vcpus=2, memory_mb=2048, local_gb=128, vcpus_used=0,
                        memory_mb_used=0, local_gb_used=0, cpu_info='{}',
                        hypervisor_type='ironic', hypervisor_version=10,
                        hypervisor_hostname=nodename)
                    node.create()

        # create a compute node for VMs along with a corresponding nova-compute
        # service host in cell1
        with context.target_cell(self.ctxt, cell1) as cctxt:
            hostname = 'vm_host_cell1_1'
            svc = objects.Service(cctxt, host=hostname,
                                  binary='nova-compute',
                                  topic='nova-compute')
            svc.create()
            compute_node_uuid = getattr(uuids, hostname)
            node = objects.ComputeNode(
                cctxt, uuid=compute_node_uuid, host=hostname,
                vcpus=2, memory_mb=2048, local_gb=128, vcpus_used=0,
                memory_mb_used=0, local_gb_used=0, cpu_info='{}',
                hypervisor_type='libvirt', hypervisor_version=10,
                hypervisor_hostname=hostname)
            node.create()

    def test_service_hostname(self):
        """Test to make sure we can associate and disassociate an aggregate
        with a service host.
        """
        agg = objects.Aggregate(self.ctxt, name="rack1_baremetal")
        agg.create()

        agg_id = agg.id

        # There is no such service host called unknown_host_cell1, so should
        # get back a ComputeHostNotFound
        self.assertRaises(exception.ComputeHostNotFound,
                          self.agg_api.add_host_to_aggregate, self.ctxt,
                          agg_id, 'unknown_host_cell1')
        self.assertRaises(exception.ComputeHostNotFound,
                          self.agg_api.remove_host_from_aggregate, self.ctxt,
                          agg_id, 'unknown_host_cell1')

        hosts = ('ironic_host_cell1', 'ironic_host_cell2', 'vm_host_cell1_1')
        for service_host in hosts:
            self.agg_api.add_host_to_aggregate(self.ctxt, agg_id, service_host)
            self.agg_api.remove_host_from_aggregate(self.ctxt, agg_id,
                                                    service_host)

    def test_compute_nodename(self):
        """Test to make sure we can associate and disassociate an aggregate
        with a compute node by its hypervisor_hostname.
        """
        agg = objects.Aggregate(self.ctxt, name="rack1_baremetal")
        agg.create()

        agg_id = agg.id

        # There is no such compute node called unknown_host_cell1, so should
        # get back a ComputeHostNotFound
        self.assertRaises(exception.ComputeHostNotFound,
                          self.agg_api.add_host_to_aggregate, self.ctxt,
                          agg_id, getattr(uuids, 'unknown_node_cell1'))
        self.assertRaises(exception.ComputeHostNotFound,
                          self.agg_api.remove_host_from_aggregate, self.ctxt,
                          agg_id, getattr(uuids, 'unknown_host_cell1'))

        nodenames = ('ironic_node_cell1_2', 'ironic_node_cell2_1',
                 'vm_host_cell1_1')
        for nodename in nodenames:
            self.agg_api.add_host_to_aggregate(self.ctxt, agg_id, nodename)
            self.agg_api.remove_host_from_aggregate(self.ctxt, agg_id,
                                                    nodename)
