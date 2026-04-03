"""
Proactive Monitor — Background anomaly detection for ICRM operations.

Runs periodic checks against Shiprocket data and generates alerts when
anomalies are detected. Alerts are stored in cosmos_proactive_alerts table
and surfaced in the Lime dashboard.

Monitors:
  - NDR spike detection (by courier/region)
  - Pickup failure patterns (by seller)
  - Weight dispute surges (by courier)
  - COD remittance delays
  - Seller wallet low balance

Inspired by Microsoft Copilot's Autonomous Triggers pattern.

Usage:
    monitor = ProactiveMonitor()
    await monitor.run_all_checks()
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()


@dataclass
class Alert:
    monitor_name: str
    severity: str  # low, medium, high, critical
    title: str
    description: str
    data: Dict[str, Any] = field(default_factory=dict)
    affected_entities: List[str] = field(default_factory=list)


@dataclass
class MonitorConfig:
    name: str
    description: str
    check_interval_minutes: int
    severity: str
    enabled: bool = True


MONITORS = [
    MonitorConfig(
        name="ndr_spike",
        description="Detects unusual NDR rate spikes by courier or region",
        check_interval_minutes=15,
        severity="high",
    ),
    MonitorConfig(
        name="pickup_failure",
        description="Detects sellers with repeated pickup failures",
        check_interval_minutes=30,
        severity="medium",
    ),
    MonitorConfig(
        name="weight_dispute_surge",
        description="Detects courier-level weight dispute surges",
        check_interval_minutes=60,
        severity="medium",
    ),
    MonitorConfig(
        name="cod_remittance_delay",
        description="Detects COD remittance delays beyond SLA",
        check_interval_minutes=360,
        severity="high",
    ),
    MonitorConfig(
        name="wallet_low_balance",
        description="Detects sellers with critically low wallet balance",
        check_interval_minutes=30,
        severity="medium",
    ),
]


class ProactiveMonitor:
    """Runs background anomaly detection and generates alerts."""

    def __init__(self):
        self._last_run: Dict[str, float] = {}

    async def run_all_checks(self) -> List[Alert]:
        """Run all enabled monitors and return generated alerts."""
        all_alerts = []

        for config in MONITORS:
            if not config.enabled:
                continue

            # Check if enough time has passed since last run
            last = self._last_run.get(config.name, 0)
            if time.time() - last < config.check_interval_minutes * 60:
                continue

            try:
                alerts = await self._run_monitor(config)
                if alerts:
                    for alert in alerts:
                        await self._store_alert(alert)
                    all_alerts.extend(alerts)
                    logger.info("proactive_monitor.alerts_generated",
                                monitor=config.name, count=len(alerts))
                self._last_run[config.name] = time.time()
            except Exception as e:
                logger.warning("proactive_monitor.check_failed",
                               monitor=config.name, error=str(e))

        return all_alerts

    async def _run_monitor(self, config: MonitorConfig) -> List[Alert]:
        """Run a specific monitor check."""
        # Each monitor queries the MARS DB for anomalies.
        # In production, these would query actual Shiprocket tables.
        # For now, they check graph_nodes + metadata for patterns.

        if config.name == "ndr_spike":
            return await self._check_ndr_spike()
        elif config.name == "pickup_failure":
            return await self._check_pickup_failure()
        elif config.name == "weight_dispute_surge":
            return await self._check_weight_dispute_surge()
        elif config.name == "cod_remittance_delay":
            return await self._check_cod_remittance_delay()
        elif config.name == "wallet_low_balance":
            return await self._check_wallet_low_balance()

        return []

    async def _check_ndr_spike(self) -> List[Alert]:
        """Check for NDR rate spikes. In production, queries shipment events table."""
        # Placeholder: this would query actual shipment data
        # For now, returns empty (no alerts) until connected to live data
        return []

    async def _check_pickup_failure(self) -> List[Alert]:
        """Check for repeated pickup failures by seller."""
        return []

    async def _check_weight_dispute_surge(self) -> List[Alert]:
        """Check for weight dispute surges by courier."""
        return []

    async def _check_cod_remittance_delay(self) -> List[Alert]:
        """Check for COD remittance delays beyond SLA."""
        return []

    async def _check_wallet_low_balance(self) -> List[Alert]:
        """Check for sellers with critically low wallet balance."""
        return []

    async def _store_alert(self, alert: Alert):
        """Store alert in cosmos_proactive_alerts table."""
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""INSERT INTO cosmos_proactive_alerts
                            (id, monitor_name, severity, title, description, data, affected_entities, status)
                            VALUES (:id, :monitor, :sev, :title, :desc, :data, :entities, 'active')"""),
                    {
                        "id": str(uuid.uuid4()),
                        "monitor": alert.monitor_name,
                        "sev": alert.severity,
                        "title": alert.title,
                        "desc": alert.description,
                        "data": json.dumps(alert.data, default=str),
                        "entities": json.dumps(alert.affected_entities),
                    },
                )
                await session.commit()
        except Exception as e:
            logger.debug("proactive_monitor.store_alert_failed", error=str(e))

    async def get_active_alerts(self, limit: int = 20) -> List[Dict]:
        """Get active alerts for display in Lime dashboard."""
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("""SELECT id, monitor_name, severity, title, description, data,
                                   affected_entities, status, created_at
                            FROM cosmos_proactive_alerts
                            WHERE status = 'active'
                            ORDER BY
                                FIELD(severity, 'critical', 'high', 'medium', 'low'),
                                created_at DESC
                            LIMIT :lim"""),
                    {"lim": limit},
                )
                alerts = []
                for row in result.fetchall():
                    alerts.append({
                        "id": row.id,
                        "monitor_name": row.monitor_name,
                        "severity": row.severity,
                        "title": row.title,
                        "description": row.description,
                        "data": json.loads(row.data) if row.data else {},
                        "affected_entities": json.loads(row.affected_entities) if row.affected_entities else [],
                        "created_at": str(row.created_at),
                    })
                return alerts
        except Exception as e:
            logger.debug("proactive_monitor.get_alerts_failed", error=str(e))
            return []

    def get_monitor_configs(self) -> List[Dict]:
        """Return monitor configurations for display."""
        return [
            {
                "name": m.name,
                "description": m.description,
                "check_interval_minutes": m.check_interval_minutes,
                "severity": m.severity,
                "enabled": m.enabled,
            }
            for m in MONITORS
        ]
