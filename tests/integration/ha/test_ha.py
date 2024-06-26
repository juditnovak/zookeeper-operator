#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import logging
from subprocess import CalledProcessError

import continuous_writes as cw
import helpers
import pytest
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

USERNAME = "super"

CLIENT_TIMEOUT = 10
RESTART_DELAY = 60


@pytest.fixture()
async def restart_delay(ops_test: OpsTest):
    for unit in ops_test.model.applications[helpers.APP_NAME].units:
        await helpers.patch_restart_delay(
            ops_test=ops_test, unit_name=unit.name, delay=RESTART_DELAY
        )
    yield
    for unit in ops_test.model.applications[helpers.APP_NAME].units:
        await helpers.remove_restart_delay(ops_test=ops_test, unit_name=unit.name)


@pytest.fixture()
async def no_lxd_dnsmasq():
    helpers.disable_lxd_dnsmasq()
    yield
    helpers.enable_lxd_dnsmasq()


@pytest.mark.skip_if_deployed
@pytest.mark.abort_on_fail
async def test_deploy_active_no_dnsmasq(ops_test: OpsTest, no_lxd_dnsmasq):
    """Tests that the charm deploys safely, without DNS resolution from LXD dnsmasq."""
    charm = await ops_test.build_charm(".")
    await ops_test.model.deploy(
        charm,
        application_name=helpers.APP_NAME,
        num_units=3,
        storage={"data": {"pool": "lxd-btrfs", "size": 10240}},
    )
    await helpers.wait_idle(ops_test)

    assert helpers.ping_servers(ops_test)


@pytest.mark.abort_on_fail
async def test_kill_db_process(ops_test: OpsTest, request, restart_delay):
    """SIGKILLs leader process and checks recovery + re-election."""
    hosts = helpers.get_hosts(ops_test)
    leader_name = helpers.get_leader_name(ops_test, hosts)
    leader_host = helpers.get_unit_host(ops_test, leader_name)
    password = await helpers.get_password(ops_test)
    parent = request.node.name
    non_leader_hosts = ",".join([host for host in hosts.split(",") if host != leader_host])

    logger.info("Starting continuous_writes...")
    cw.start_continuous_writes(parent=parent, hosts=hosts, username=USERNAME, password=password)
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # letting client set up and start writing

    logger.info("Checking writes are running at all...")
    assert cw.count_znodes(parent=parent, hosts=hosts, username=USERNAME, password=password)

    logger.info("Killing leader process...")
    await helpers.send_control_signal(ops_test=ops_test, unit_name=leader_name, signal="SIGKILL")

    # Check that process is down
    assert await helpers.is_down(ops_test=ops_test, unit=leader_name)

    await asyncio.sleep(RESTART_DELAY * 2)

    logger.info("Checking writes are increasing...")
    writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # increasing writes
    new_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert new_writes > writes, "writes not continuing to ZK"

    logger.info("Checking leader re-election...")
    new_leader_name = helpers.get_leader_name(ops_test, non_leader_hosts)
    assert new_leader_name != leader_name

    logger.info("Stopping continuous_writes...")
    cw.stop_continuous_writes()

    logger.info("Counting writes on surviving units...")
    last_write = cw.get_last_znode(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    total_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert last_write == total_writes

    logger.info("Checking old leader caught up...")
    last_write_leader = cw.get_last_znode(
        parent=parent, hosts=leader_host, username=USERNAME, password=password
    )
    total_writes_leader = cw.count_znodes(
        parent=parent, hosts=leader_host, username=USERNAME, password=password
    )
    assert last_write == last_write_leader
    assert total_writes == total_writes_leader


@pytest.mark.abort_on_fail
async def test_restart_db_process(ops_test: OpsTest, request):
    """SIGTERMSs leader process and checks recovery + re-election."""
    hosts = helpers.get_hosts(ops_test)
    leader_name = helpers.get_leader_name(ops_test, hosts)
    leader_host = helpers.get_unit_host(ops_test, leader_name)
    password = await helpers.get_password(ops_test)
    parent = request.node.name
    non_leader_hosts = ",".join([host for host in hosts.split(",") if host != leader_host])

    logger.info("Starting continuous_writes...")
    cw.start_continuous_writes(parent=parent, hosts=hosts, username=USERNAME, password=password)
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # letting client set up and start writing

    logger.info("Checking writes are running at all...")
    assert cw.count_znodes(parent=parent, hosts=hosts, username=USERNAME, password=password)

    logger.info("Killing leader process...")
    await helpers.send_control_signal(ops_test=ops_test, unit_name=leader_name, signal="SIGTERM")

    logger.info("Checking writes are increasing...")
    writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # increasing writes
    new_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert new_writes > writes, "writes not continuing to ZK"

    logger.info("Checking leader re-election...")
    new_leader_name = helpers.get_leader_name(ops_test, non_leader_hosts)
    assert new_leader_name != leader_name

    logger.info("Stopping continuous_writes...")
    cw.stop_continuous_writes()

    logger.info("Counting writes on surviving units...")
    last_write = cw.get_last_znode(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    total_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert last_write == total_writes

    logger.info("Checking old leader caught up...")
    last_write_leader = cw.get_last_znode(
        parent=parent, hosts=leader_host, username=USERNAME, password=password
    )
    total_writes_leader = cw.count_znodes(
        parent=parent, hosts=leader_host, username=USERNAME, password=password
    )
    assert last_write == last_write_leader
    assert total_writes == total_writes_leader


@pytest.mark.abort_on_fail
async def test_freeze_db_process(ops_test: OpsTest, request):
    """SIGSTOPs leader process and checks recovery + re-election after SIGCONT."""
    hosts = helpers.get_hosts(ops_test)
    leader_name = helpers.get_leader_name(ops_test, hosts)
    leader_host = helpers.get_unit_host(ops_test, leader_name)
    password = await helpers.get_password(ops_test)
    parent = request.node.name
    non_leader_hosts = ",".join([host for host in hosts.split(",") if host != leader_host])

    logger.info("Starting continuous_writes...")
    cw.start_continuous_writes(parent=parent, hosts=hosts, username=USERNAME, password=password)
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # letting client set up and start writing

    logger.info("Checking writes are running at all...")
    assert cw.count_znodes(parent=parent, hosts=hosts, username=USERNAME, password=password)

    logger.info("Stopping leader process...")
    await helpers.send_control_signal(ops_test=ops_test, unit_name=leader_name, signal="SIGSTOP")
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # to give time for re-election

    logger.info("Checking writes are increasing...")
    writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # increasing writes
    new_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert new_writes > writes, "writes not continuing to ZK"

    logger.info("Checking leader re-election...")
    new_leader_name = helpers.get_leader_name(ops_test, non_leader_hosts)
    assert new_leader_name != leader_name

    logger.info("Continuing leader process...")
    await helpers.send_control_signal(ops_test=ops_test, unit_name=leader_name, signal="SIGCONT")
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # letting writes continue while unit rejoins

    logger.info("Stopping continuous_writes...")
    cw.stop_continuous_writes()
    await asyncio.sleep(CLIENT_TIMEOUT)  # buffer to ensure writes sync

    logger.info("Counting writes on surviving units...")
    last_write = cw.get_last_znode(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    total_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert last_write == total_writes

    logger.info("Checking old leader caught up...")
    last_write_leader = cw.get_last_znode(
        parent=parent, hosts=leader_host, username=USERNAME, password=password
    )
    total_writes_leader = cw.count_znodes(
        parent=parent, hosts=leader_host, username=USERNAME, password=password
    )
    assert last_write == last_write_leader
    assert total_writes == total_writes_leader


@pytest.mark.abort_on_fail
async def test_network_cut_without_ip_change(ops_test: OpsTest, request):
    """Cuts and restores network on leader, cluster self-heals after IP change."""
    hosts = helpers.get_hosts(ops_test)
    leader_name = helpers.get_leader_name(ops_test, hosts)
    initial_leader_host = helpers.get_unit_host(ops_test, leader_name)
    leader_machine_name = await helpers.get_unit_machine_name(ops_test, leader_name)
    password = await helpers.get_password(ops_test)
    parent = request.node.name
    non_leader_hosts = ",".join([host for host in hosts.split(",") if host != initial_leader_host])

    logger.info("Starting continuous_writes...")
    cw.start_continuous_writes(parent=parent, hosts=hosts, username=USERNAME, password=password)
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # letting client set up and start writing

    logger.info("Checking writes are running at all...")
    assert cw.count_znodes(parent=parent, hosts=hosts, username=USERNAME, password=password)

    logger.info("Cutting leader network...")
    helpers.network_throttle(machine_name=leader_machine_name)
    await asyncio.sleep(CLIENT_TIMEOUT * 3)

    logger.info("Checking writes are increasing...")
    writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # increasing writes
    new_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert new_writes > writes, "writes not continuing to ZK"

    logger.info("Checking leader re-election...")
    new_leader_name = helpers.get_leader_name(ops_test, non_leader_hosts)
    assert new_leader_name != leader_name

    logger.info("Restoring leader network...")
    helpers.network_release(machine_name=leader_machine_name)
    await asyncio.sleep(CLIENT_TIMEOUT * 6)  # Give time for unit to rejoin

    logger.info("Stopping continuous_writes...")
    cw.stop_continuous_writes()

    logger.info("Counting writes on surviving units...")
    last_write = cw.get_last_znode(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    total_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert last_write == total_writes

    logger.info("Checking old leader caught up...")
    last_write_leader = cw.get_last_znode(
        parent=parent, hosts=initial_leader_host, username=USERNAME, password=password
    )
    total_writes_leader = cw.count_znodes(
        parent=parent, hosts=initial_leader_host, username=USERNAME, password=password
    )
    assert last_write == last_write_leader
    assert total_writes == total_writes_leader


async def test_full_cluster_crash(ops_test: OpsTest, request, restart_delay):
    hosts = helpers.get_hosts(ops_test)
    leader_name = helpers.get_leader_name(ops_test, hosts)
    leader_host = helpers.get_unit_host(ops_test, leader_name)
    password = await helpers.get_password(ops_test)
    parent = request.node.name
    non_leader_hosts = ",".join([host for host in hosts.split(",") if host != leader_host])

    logger.info("Starting continuous_writes...")
    cw.start_continuous_writes(parent=parent, hosts=hosts, username=USERNAME, password=password)
    await asyncio.sleep(CLIENT_TIMEOUT * 3)

    logger.info("Counting writes are running at all...")
    assert cw.count_znodes(parent=parent, hosts=hosts, username=USERNAME, password=password)

    # kill all units "simultaneously"
    await asyncio.gather(
        *[
            helpers.send_control_signal(ops_test, unit.name, signal="SIGKILL")
            for unit in ops_test.model.applications[helpers.APP_NAME].units
        ]
    )

    # Check that all servers are down at the same time
    assert await helpers.all_db_processes_down(ops_test), "Not all units down at the same time."

    await asyncio.sleep(RESTART_DELAY * 2)

    logger.info("Checking writes are increasing...")
    writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # letting client set up and start writing
    new_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert new_writes > writes, "writes not continuing to ZK"

    logger.info("Stopping continuous_writes...")
    cw.stop_continuous_writes()

    logger.info("Counting writes on surviving units...")
    last_write = cw.get_last_znode(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    total_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert last_write == total_writes


async def test_full_cluster_restart(ops_test: OpsTest, request):
    hosts = helpers.get_hosts(ops_test)
    leader_name = helpers.get_leader_name(ops_test, hosts)
    leader_host = helpers.get_unit_host(ops_test, leader_name)
    password = await helpers.get_password(ops_test)
    parent = request.node.name
    non_leader_hosts = ",".join([host for host in hosts.split(",") if host != leader_host])

    logger.info("Starting continuous_writes...")
    cw.start_continuous_writes(parent=parent, hosts=hosts, username=USERNAME, password=password)
    await asyncio.sleep(CLIENT_TIMEOUT)

    logger.info("Counting writes are running at all...")
    assert cw.count_znodes(parent=parent, hosts=hosts, username=USERNAME, password=password)

    # kill all units "simultaneously"
    await asyncio.gather(
        *[
            helpers.send_control_signal(ops_test, unit.name, signal="SIGTERM")
            for unit in ops_test.model.applications[helpers.APP_NAME].units
        ]
    )
    await asyncio.sleep(CLIENT_TIMEOUT)

    logger.info("Checking writes are increasing...")
    writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # letting client set up and start writing
    new_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert new_writes > writes, "writes not continuing to ZK"

    logger.info("Stopping continuous_writes...")
    cw.stop_continuous_writes()

    logger.info("Counting writes on surviving units...")
    last_write = cw.get_last_znode(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    total_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert last_write == total_writes


@pytest.mark.abort_on_fail
@pytest.mark.skip(
    reason="Did not play nice with pytest-operator. Issue https://github.com/canonical/zookeeper-operator/issues/85"
)
async def test_scale_down_storage_re_use(ops_test: OpsTest, request):
    hosts = helpers.get_hosts(ops_test)
    password = await helpers.get_password(ops_test)
    parent = request.node.name

    logger.info("Starting continuous_writes...")
    cw.start_continuous_writes(parent=parent, hosts=hosts, username=USERNAME, password=password)
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # letting client set up and start writing

    logger.info("Checking writes are running at all...")
    assert cw.count_znodes(parent=parent, hosts=hosts, username=USERNAME, password=password)

    logger.info("Stopping continuous_writes...")
    cw.stop_continuous_writes()
    await helpers.wait_idle(ops_test)

    logger.info("Scaling down unit...")
    unit_to_remove = ops_test.model.applications[helpers.APP_NAME].units[0]
    unit_storage_id = helpers.get_storage_id(ops_test, unit_name=unit_to_remove.name)
    await ops_test.model.applications[helpers.APP_NAME].destroy_units(unit_to_remove.name)
    await helpers.wait_idle(ops_test, units=2)

    old_units = [unit.name for unit in ops_test.model.applications[helpers.APP_NAME].units]

    logger.info("Scaling down and up and reusing storage...")
    await helpers.reuse_storage(ops_test, unit_storage_id=unit_storage_id)

    new_units = [unit.name for unit in ops_test.model.applications[helpers.APP_NAME].units]
    added_unit_name = list(set(new_units) - set(old_units))[0]

    logger.info("Verifying storage reuse...")
    assert helpers.get_storage_id(ops_test, unit_name=added_unit_name) == unit_storage_id

    # long sleep to ensure CI can provision resources and fully set-up
    await asyncio.sleep(CLIENT_TIMEOUT * 10)

    new_host = helpers.get_unit_host(ops_test, added_unit_name)

    logger.info("Confirming writes replicated on new unit...")
    assert cw.count_znodes(parent=parent, hosts=new_host, username=USERNAME, password=password)


@pytest.mark.abort_on_fail
@pytest.mark.unstable(
    reason="Causes pytest-operator to be unstable. Hostname behaviour is somewhat tested during test_deploy_active_no_dnsmasq"
)
async def test_network_cut_self_heal(ops_test: OpsTest, request):
    """Cuts and restores network on leader, cluster self-heals after IP change."""
    hosts = helpers.get_hosts(ops_test)
    leader_name = helpers.get_leader_name(ops_test, hosts)
    leader_host = helpers.get_unit_host(ops_test, leader_name)
    leader_machine_name = await helpers.get_unit_machine_name(ops_test, leader_name)
    password = await helpers.get_password(ops_test)
    parent = request.node.name
    non_leader_hosts = ",".join([host for host in hosts.split(",") if host != leader_host])

    # cleans up potential leftover device config from other tests
    try:
        helpers.restore_unit_network(machine_name=leader_machine_name)
    except CalledProcessError:  # in case it was already cleaned up
        pass

    logger.info("Starting continuous_writes...")
    cw.start_continuous_writes(parent=parent, hosts=hosts, username=USERNAME, password=password)
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # letting client set up and start writing

    logger.info("Checking writes are running at all...")
    assert cw.count_znodes(parent=parent, hosts=hosts, username=USERNAME, password=password)

    logger.info("Cutting leader network...")
    helpers.cut_unit_network(machine_name=leader_machine_name)
    await asyncio.sleep(
        CLIENT_TIMEOUT * 6
    )  # to give time for re-election, longer as network cut is weird

    logger.info("Checking writes are increasing...")
    writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    await asyncio.sleep(CLIENT_TIMEOUT * 3)  # increasing writes
    new_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert new_writes > writes, "writes not continuing to ZK"

    logger.info("Checking leader re-election...")
    new_leader_name = helpers.get_leader_name(ops_test, non_leader_hosts)
    assert new_leader_name != leader_name

    logger.info("Restoring leader network...")
    helpers.restore_unit_network(machine_name=leader_machine_name)

    logger.info("Waiting for Juju to detect new IP...")
    await ops_test.model.block_until(
        lambda: leader_host
        not in helpers.get_hosts_from_status(ops_test),  # ip changes after lxd config add
        timeout=1200,
        wait_period=5,
    )

    logger.info("Stopping continuous_writes...")
    cw.stop_continuous_writes()

    logger.info("Counting writes on surviving units...")
    last_write = cw.get_last_znode(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    total_writes = cw.count_znodes(
        parent=parent, hosts=non_leader_hosts, username=USERNAME, password=password
    )
    assert last_write == total_writes

    new_hosts = helpers.get_hosts_from_status(ops_test)
    new_leader_host = max(set(new_hosts) - set(hosts))

    logger.info("Checking old leader caught up...")
    last_write_leader = cw.get_last_znode(
        parent=parent, hosts=new_leader_host, username=USERNAME, password=password
    )
    total_writes_leader = cw.count_znodes(
        parent=parent, hosts=new_leader_host, username=USERNAME, password=password
    )
    assert last_write == last_write_leader
    assert total_writes == total_writes_leader
