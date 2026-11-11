"""Simulated OPC UA server for the gantry stepper motor (FR-1).

Address space (custom namespace ``urn:bioprint:guard``)::

    Objects
    └── FactoryFloor
        └── ExtrusionGantry
            └── StepperMotor
                ├── motor_current        Double, A
                ├── housing_temp         Double, °C
                └── vibration_frequency  Double, Hz

Every sample writes the three variables with the **same source timestamp**, so
a client can reassemble synchronous readings from per-variable data-change
notifications. Until the first sample is published the variables carry the
status ``BadWaitingForInitialData``.

Run it on its own (from the project folder)::

    python src/opcua_server.py                    # 10 Hz, wear fault after 15 s, runs until Ctrl-C
    python src/opcua_server.py --duration 30      # stop after 30 simulated seconds
    python src/opcua_server.py --speed 10         # 10x faster than real time (same data)
    python src/opcua_server.py --start-delay 5    # give a client 5 s to subscribe first

The server has no security policy and accepts anonymous sessions. That is a
demo setting; a real deployment would use Basic256Sha256 with certificates.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import sys
from datetime import datetime, timezone

from asyncua import Server, ua
from motor_model import SIGNALS, MotorSample, StepperMotorSimulator

DEFAULT_ENDPOINT = "opc.tcp://0.0.0.0:4840/bioprint/"
NAMESPACE_URI = "urn:bioprint:guard"
NODE_PATH: tuple[str, ...] = ("FactoryFloor", "ExtrusionGantry", "StepperMotor")

# asyncua 1.x names the DataValue status field ``StatusCode_``; 2.x renamed it ``StatusCode``.
STATUS_FIELD = (
    "StatusCode"
    if "StatusCode" in {f.name for f in dataclasses.fields(ua.DataValue)}
    else "StatusCode_"
)

log = logging.getLogger("bioprint_guard.server")


def data_value(value: float, status: int, source_timestamp: datetime | None = None) -> ua.DataValue:
    """Build a Double DataValue with an explicit status and source timestamp."""
    return ua.DataValue(
        ua.Variant(value, ua.VariantType.Double),
        SourceTimestamp=source_timestamp,
        **{STATUS_FIELD: ua.StatusCode(status)},
    )


class MotorOpcUaServer:
    """OPC UA server that publishes :class:`StepperMotorSimulator` samples.

    Args:
        simulator: Signal source.
        endpoint: Endpoint URL to listen on.
        speed: Wall-clock speed-up. 1.0 publishes in real time (10 Hz for the
            default 0.1 s sample period); 10.0 publishes the same samples 10x faster.
    """

    def __init__(
        self,
        simulator: StepperMotorSimulator,
        endpoint: str = DEFAULT_ENDPOINT,
        speed: float = 1.0,
    ) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.simulator = simulator
        self.endpoint = endpoint
        self.speed = speed
        self.samples_published = 0
        self._server: Server | None = None
        self._variables: dict[str, object] = {}

    async def start(self) -> None:
        """Build the address space and start listening."""
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("BioPrint-Guard simulated motor")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        namespace = await server.register_namespace(NAMESPACE_URI)

        parent = server.nodes.objects
        for name in NODE_PATH:
            parent = await parent.add_object(namespace, name)
        waiting = data_value(0.0, ua.StatusCodes.BadWaitingForInitialData)
        for signal in SIGNALS:
            variable = await parent.add_variable(namespace, signal, 0.0, ua.VariantType.Double)
            await variable.write_value(waiting)
            self._variables[signal] = variable

        await server.start()
        self._server = server
        log.info("OPC UA server listening on %s (%s)", self.endpoint, "/".join(NODE_PATH))

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None
            log.info("OPC UA server stopped after %d samples", self.samples_published)

    async def __aenter__(self) -> MotorOpcUaServer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def publish_sample(self, sample: MotorSample) -> None:
        """Write one reading: three variables, one shared source timestamp."""
        for signal, value in zip(SIGNALS, sample.values(), strict=True):
            reading = data_value(value, ua.StatusCodes.Good, sample.timestamp)
            await self._variables[signal].write_value(reading)  # type: ignore[attr-defined]
        self.samples_published += 1

    async def publish(self, n_samples: int | None = None) -> None:
        """Publish ``n_samples`` readings (or forever) at the configured rate.

        Each sample is scheduled against the start time, not the previous
        sample, so the rate does not drift.
        """
        loop = asyncio.get_running_loop()
        period = self.simulator.sample_period_s / self.speed
        started = loop.time()
        count = 0
        while n_samples is None or count < n_samples:
            await self.publish_sample(self.simulator.read())
            count += 1
            await asyncio.sleep(max(0.0, started + count * period - loop.time()))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BioPrint-Guard simulated OPC UA motor server")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="endpoint URL to listen on")
    parser.add_argument(
        "--duration", type=float, default=None, help="simulated seconds to run (default: forever)"
    )
    parser.add_argument(
        "--fault-after", type=float, default=15.0, help="wear onset, simulated seconds (default 15)"
    )
    parser.add_argument("--healthy", action="store_true", help="never inject the wear fault")
    parser.add_argument("--speed", type=float, default=1.0, help="wall-clock speed-up (default 1)")
    parser.add_argument("--seed", type=int, default=42, help="simulator random seed")
    parser.add_argument(
        "--start-delay",
        type=float,
        default=0.0,
        help="seconds to wait before the first sample, so a client can subscribe first (default 0)",
    )
    parser.add_argument(
        "--linger",
        type=float,
        default=2.0,
        help="seconds to keep serving after the last sample, so clients can drain (default 2)",
    )
    return parser.parse_args(argv)


async def serve(args: argparse.Namespace) -> None:
    simulator = StepperMotorSimulator(
        seed=args.seed,
        fault_after_s=None if args.healthy else args.fault_after,
        epoch=datetime.now(timezone.utc),
    )
    n_samples = None if args.duration is None else round(args.duration / simulator.sample_period_s)
    async with MotorOpcUaServer(simulator, args.endpoint, args.speed) as server:
        await asyncio.sleep(args.start_delay)
        await server.publish(n_samples)
        await asyncio.sleep(args.linger)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
