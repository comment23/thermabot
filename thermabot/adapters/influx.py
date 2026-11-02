"""InfluxDB adapter (D§8.1-8.2). Read: Flux by `entity_id` tag (no domain prefix), any
measurement. Write: line protocol to the bot's own bucket, buffered in memory up to 24 h."""

from __future__ import annotations

import csv
import io
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)
BUFFER_MAX_AGE = timedelta(hours=24)
BUFFER_MAX_LINES = 50_000  # hard bound; deque(maxlen) drops the oldest line first


def strip_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[1] if "." in entity_id else entity_id


def _flux_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def flux_query(
    bucket: str,
    entity_id: str,
    fields: list[str],
    start: str = "-60d",
    every: str = "5m",
    stop: str | None = None,
) -> str:
    fcond = " or ".join(f"r._field == {_flux_str(f)}" for f in fields)
    rng = f"start: {start}" + (f", stop: {stop}" if stop else "")
    return (
        f"from(bucket:{_flux_str(bucket)}) |> range({rng})\n"
        f"  |> filter(fn:(r)=> r.entity_id == {_flux_str(strip_domain(entity_id))}"
        f" and ({fcond}))\n"
        f"  |> aggregateWindow(every: {every}, fn: last, createEmpty: false)"
    )


def parse_csv(text: str) -> list[dict[str, Any]]:
    """Annotated-CSV -> rows {t, field, value, measurement}. Multiple tables are fine."""
    out: list[dict[str, Any]] = []
    header: list[str] | None = None
    for row in csv.reader(io.StringIO(text)):
        if not row or (len(row) == 1 and not row[0]):
            header = None
            continue
        if row[0].startswith("#"):
            continue
        if header is None:
            header = row
            continue
        r = dict(zip(header, row, strict=False))
        if "_time" not in r or "_value" not in r:
            continue
        v: Any = r["_value"]
        try:
            v = float(v)
        except ValueError:
            pass
        out.append(
            {
                "t": datetime.fromisoformat(r["_time"].replace("Z", "+00:00")),
                "field": r.get("_field", ""),
                "value": v,
                "measurement": r.get("_measurement", ""),
            }
        )
    return out


def _esc(s: str, chars: str) -> str:
    for c in ("\\", *chars):
        s = s.replace(c, "\\" + c)
    return s


def line(
    measurement: str, tags: dict[str, str], fields: dict[str, float | int | str | bool], t: datetime
) -> str:
    m = _esc(measurement, ", ")
    tg = "".join(f",{_esc(k, ',= ')}={_esc(v, ',= ')}" for k, v in sorted(tags.items()))
    fs: list[str] = []
    for k, v in fields.items():
        if isinstance(v, bool):
            val = "true" if v else "false"
        elif isinstance(v, int):
            val = f"{v}i"
        elif isinstance(v, float):
            val = repr(v)
        else:
            val = '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
        fs.append(f"{_esc(k, ',= ')}={val}")
    ns = int(t.timestamp() * 1_000_000_000)
    return f"{m}{tg} {','.join(fs)} {ns}"


class Influx:
    def __init__(
        self,
        url: str,
        org: str,
        read_bucket: str,
        write_bucket: str,
        read_token: str | None,
        write_token: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.org, self.read_bucket, self.write_bucket = org, read_bucket, write_bucket
        self._read_tok, self._write_tok = read_token, write_token
        self._http = httpx.AsyncClient(base_url=url, timeout=30, transport=transport)
        self.buffer: deque[tuple[datetime, str]] = deque(maxlen=BUFFER_MAX_LINES)
        self.last_error: str | None = None

    @property
    def readable(self) -> bool:
        return bool(self._read_tok)

    @property
    def writable(self) -> bool:
        return bool(self._write_tok)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def query(self, flux: str) -> list[dict[str, Any]]:
        if not self._read_tok:
            raise RuntimeError("INFLUX_READ_TOKEN not set")
        r = await self._http.post(
            "/api/v2/query",
            params={"org": self.org},
            content=flux,
            headers={
                "Authorization": f"Token {self._read_tok}",
                "Content-Type": "application/vnd.flux",
                "Accept": "application/csv",
            },
        )
        r.raise_for_status()
        return parse_csv(r.text)

    async def read_entity(
        self,
        entity_id: str,
        fields: list[str],
        start: str = "-60d",
        every: str = "5m",
        stop: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.query(flux_query(self.read_bucket, entity_id, fields, start, every, stop))

    def enqueue(self, lines: list[str], now: datetime | None = None) -> None:
        t = now or datetime.now(UTC)
        self.buffer.extend((t, ln) for ln in lines)

    async def flush(self, now: datetime | None = None) -> bool:
        """POST buffered lines. On failure keep them (drop > 24 h old); never raises."""
        t = now or datetime.now(UTC)
        while self.buffer and t - self.buffer[0][0] > BUFFER_MAX_AGE:
            self.buffer.popleft()
        if not self.buffer:
            return True
        if not self._write_tok:
            self.last_error = "INFLUX_WRITE_TOKEN not set"
            return False
        body = "\n".join(ln for _, ln in self.buffer)
        try:
            r = await self._http.post(
                "/api/v2/write",
                params={"org": self.org, "bucket": self.write_bucket, "precision": "ns"},
                content=body.encode(),
                headers={
                    "Authorization": f"Token {self._write_tok}",
                    "Content-Type": "text/plain; charset=utf-8",
                },
            )
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 - write-back must never affect control
            self.last_error = type(e).__name__
            log.warning("influx write failed (%s); %d lines buffered", e, len(self.buffer))
            return False
        self.buffer.clear()
        self.last_error = None
        return True
