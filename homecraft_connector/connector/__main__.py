import asyncio
import logging
import sys

from . import audit, errors, fingerprint, identity, panel, selfcheck, settings, state, system_log
from .client import Client

logger = logging.getLogger("connector")


async def main() -> None:
    opts = settings.options()
    server_url = opts["server_url"]
    if settings.lan_ws(server_url):
        logger.warning(
            "Spojení s Homecraftem není šifrované (ws:// na adresu v LAN): jen pro test, "
            "přístupový token jde po síti nešifrovaně"
        )
    elif not server_url.startswith("wss://") and not settings.DEV:
        logger.error(
            "server_url musí začínat wss:// (ws:// jen na privátní IP v LAN nebo s CONNECTOR_DEV=1)"
        )
        sys.exit(1)
    agent_id, key = identity.load_or_create()
    code = identity.pairing_code(agent_id, key)
    logger.info("Homecraft Connector %s, agent %s", settings.version(), agent_id)
    logger.info("Párovací kód: %s", code)
    verification = identity.verification_code(key)
    logger.info("Ověřovací kód: %s", verification)
    audit.load()
    # The option can only switch the pause on; the panel's switch is the rest.
    paused = opts["paused"] or state.load_paused()
    if paused:
        logger.info("Homecraft je pozastaven")
    # The panel first (the verification code at once); the fingerprints are
    # looked up before each connection attempt until all are found.
    client = Client(server_url, agent_id, key, settings.version(), paused, fingerprint.Collector())
    client.error_reports = state.load_error_reports()
    await panel.start(client, code, verification)
    try:
        await selfcheck.resolve_ha_url()
        await selfcheck.run()
    except Exception:
        logger.exception("self-check selhal, pokračuji bez něj")
    # Consent on since 0.23.0: add the system_log package once (homecraftcz/homecraft#906).
    if client.error_reports and state.load_system_log()["state"] not in (
        "active",
        "own_config",
        "no_packages",
    ):
        try:
            await system_log.switch(True)  # a failure is the status `error`
        except Exception:
            logger.exception("system_log se nepodařilo nastavit")
    await asyncio.gather(client.run(), errors.Forwarder(client).run())


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(main())
