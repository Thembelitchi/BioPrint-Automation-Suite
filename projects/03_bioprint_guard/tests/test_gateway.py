"""Gateway tests: assembly and backoff units, plus real OPC UA sessions (FR-1, FR-2)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from alert_store import COMPONENT_ID, AlertResult, InMemoryAlertSink
from anomaly_detector import ALERT_THRESHOLD
from app import evaluate, free_port, run_scenario, to_frame
from gateway import Backoff, OpcUaGateway, SampleAssembler
from motor_model import SIGNALS, SIM_EPOCH, StepperMotorSimulator
from opcua_server import NAMESPACE_URI, NODE_PATH, MotorOpcUaServer

SPEED = 20.0  # publish at 200 Hz so the 30 s scenario takes about 1.5 s


def ts(i: int):
    return SIM_EPOCH + timedelta(seconds=0.1 * i)


# -- units -------------------------------------------------------------------------------


def test_backoff_doubles_to_a_ceiling_and_resets() -> None:
    backoff = Backoff(initial=0.5, factor=2.0, maximum=4.0)
    assert [backoff.next_delay() for _ in range(5)] == [0.5, 1.0, 2.0, 4.0, 4.0]
    backoff.reset()
    assert backoff.next_delay() == 0.5
    with pytest.raises(ValueError):
        Backoff(initial=0)


def test_assembler_groups_notifications_by_source_timestamp() -> None:
    assembler = SampleAssembler()
    assert assembler.add("motor_current", ts(0), 1.8) is None
    assert assembler.add("housing_temp", ts(0), 45.0) is None
    reading = assembler.add("vibration_frequency", ts(0), 120.0)
    assert reading is not None
    assert reading.timestamp == ts(0)
    assert reading.values == (1.8, 45.0, 120.0)  # always in SIGNALS order


def test_assembler_handles_interleaved_readings() -> None:
    assembler = SampleAssembler()
    out = [
        assembler.add("vibration_frequency", ts(0), 120.0),
        assembler.add("motor_current", ts(0), 1.8),
        assembler.add("motor_current", ts(1), 1.9),
        assembler.add("housing_temp", ts(0), 45.0),
        assembler.add("housing_temp", ts(1), 45.1),
        assembler.add("vibration_frequency", ts(1), 121.0),
    ]
    readings = [r for r in out if r is not None]
    assert [r.values for r in readings] == [(1.8, 45.0, 120.0), (1.9, 45.1, 121.0)]
    assert assembler.incomplete_dropped == 0


def test_assembler_ignores_values_replayed_after_a_reconnect() -> None:
    assembler = SampleAssembler()
    for signal in SIGNALS:
        assembler.add(signal, ts(5), 1.0)
    assert all(assembler.add(signal, ts(5), 1.0) is None for signal in SIGNALS)
    assert all(assembler.add(signal, ts(4), 1.0) is None for signal in SIGNALS)


def test_assembler_counts_readings_that_never_complete() -> None:
    assembler = SampleAssembler()
    assembler.add("motor_current", ts(0), 1.8)  # the other two never arrive
    for signal in SIGNALS:
        assembler.add(signal, ts(1), 1.0)
    assert assembler.incomplete_dropped == 1


# -- real OPC UA sessions on localhost ------------------------------------------------------


def test_fr1_address_space_and_variables() -> None:
    async def scenario():
        from asyncua import Client

        port = free_port()
        server = MotorOpcUaServer(
            StepperMotorSimulator(seed=1), f"opc.tcp://0.0.0.0:{port}/bioprint/"
        )
        async with server:
            sample = server.simulator.read()
            await server.publish_sample(sample)
            async with Client(f"opc.tcp://localhost:{port}/bioprint/") as client:
                namespace = await client.get_namespace_index(NAMESPACE_URI)
                motor = await client.nodes.objects.get_child(
                    [f"{namespace}:{name}" for name in NODE_PATH]
                )
                children = await motor.get_children()
                names = sorted([(await child.read_browse_name()).Name for child in children])
                values = [
                    await (await motor.get_child(f"{namespace}:{signal}")).read_value()
                    for signal in SIGNALS
                ]
        return sample, names, values

    sample, names, values = asyncio.run(scenario())
    assert NODE_PATH == ("FactoryFloor", "ExtrusionGantry", "StepperMotor")
    assert names == sorted(SIGNALS)
    assert tuple(values) == pytest.approx(sample.values())


def test_end_to_end_scenario_passes_every_requirement() -> None:
    sink = InMemoryAlertSink()
    report, server, wear = asyncio.run(run_scenario(speed=SPEED, sink=sink))
    frame = to_frame(report, wear, 0.1)
    state = asyncio.run(sink.alert_state(COMPONENT_ID))
    checks = evaluate(frame, report, server.samples_published, 15.0, state, "InMemoryAlertSink")
    assert {name: c["passed"] for name, c in checks.items()} == {
        "FR-1": True,
        "FR-2": True,
        "FR-3": True,
        "FR-4": True,
    }
    assert checks["FR-2"]["samples_lost"] == 0
    assert checks["FR-2"]["subscriptions"] == 1 and checks["FR-2"]["monitored_items"] == 3
    assert len(sink.open) == 1
    assert report.alert.severity == "CRITICAL"
    assert report.alert.peak_risk_score == pytest.approx(frame["risk_score"].max())
    assert report.alert_writes < 15  # deduplicated, not one write per sample


def test_values_survive_the_opc_ua_round_trip_exactly() -> None:
    report, _, _ = asyncio.run(run_scenario(duration_s=12.0, speed=SPEED, seed=9))
    reference = StepperMotorSimulator(seed=9)
    expected = [reference.read().values() for _ in range(120)]
    assert [r.values for r in report.records] == expected  # in order, none lost or altered


def test_healthy_motor_raises_no_alert() -> None:
    sink = InMemoryAlertSink()
    report, _, _ = asyncio.run(run_scenario(fault_after_s=None, speed=SPEED, sink=sink))
    assert report.samples == 300
    assert max(r.scored.score for r in report.records) < 0.5
    assert report.alert is None and sink.writes == 0 and not sink.open


def test_gateway_retries_with_backoff_until_the_server_appears() -> None:
    async def scenario():
        port = free_port()
        gateway = OpcUaGateway(
            f"opc.tcp://localhost:{port}/bioprint/",
            backoff=Backoff(initial=0.1, maximum=0.4),
            connect_timeout_s=1.0,
        )
        task = asyncio.create_task(gateway.run(max_samples=50, duration_s=30.0))
        while gateway.report.connect_failures < 2:  # nothing is listening yet
            await asyncio.sleep(0.05)
        server = MotorOpcUaServer(
            StepperMotorSimulator(seed=1), f"opc.tcp://0.0.0.0:{port}/bioprint/", SPEED
        )
        async with server:
            while gateway.report.subscriptions_open == 0:
                await asyncio.sleep(0.05)
            await server.publish(50)
            return await task

    report = asyncio.run(scenario())
    assert report.connect_failures >= 2
    assert report.connections == 1
    assert report.samples == 50


def test_gateway_resubscribes_after_the_server_restarts() -> None:
    async def scenario():
        port = free_port()
        simulator = StepperMotorSimulator(seed=1, fault_after_s=None)
        gateway = OpcUaGateway(
            f"opc.tcp://localhost:{port}/bioprint/",
            backoff=Backoff(initial=0.1, maximum=0.4),
            connect_timeout_s=1.0,
        )
        task = asyncio.create_task(gateway.run(max_samples=100, duration_s=40.0))
        for _ in range(2):  # two server lifetimes on the same endpoint
            connections_before = gateway.report.connections
            server = MotorOpcUaServer(simulator, f"opc.tcp://0.0.0.0:{port}/bioprint/", SPEED)
            async with server:
                while gateway.report.connections == connections_before:
                    await asyncio.sleep(0.05)
                await server.publish(50)
                await asyncio.sleep(0.3)  # let the last publish drain before stopping
        return await task

    report = asyncio.run(scenario())
    assert report.connections == 2
    assert report.connect_failures >= 1
    assert report.samples == 100
    assert [r.index for r in report.records] == list(range(100))


class SlowSink(InMemoryAlertSink):
    """A database that takes 0.3 s per write."""

    async def raise_alert(self, component_id: str, risk: float, severity: str) -> AlertResult:
        await asyncio.sleep(0.3)
        return await super().raise_alert(component_id, risk, severity)


def test_a_slow_database_never_drops_a_sample() -> None:
    sink = SlowSink()
    report, server, _ = asyncio.run(run_scenario(speed=SPEED, sink=sink))
    assert report.samples == server.samples_published == 300
    assert len(sink.open) == 1
    assert max(r.scored.score for r in report.records) > ALERT_THRESHOLD
    assert sink.open[COMPONENT_ID].peak_risk_score == pytest.approx(
        max(r.scored.score for r in report.records)
    )
    assert sink.writes <= 6  # pending alerts were merged while the sink was busy
