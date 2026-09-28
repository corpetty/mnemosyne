"""Calendars the desktop already knows: GNOME Online Accounts (Google Workspace, Microsoft 365,
Nextcloud...) and local calendars, read from evolution-data-server over D-Bus.

For work calendars that cannot publish an ICS address: sign in once in GNOME Settings -> Online
Accounts with Calendar turned on, and Mnemosyne reads those events locally. No OAuth app, no
keys. Talks D-Bus with jeepney (pure Python); on desktops without evolution-data-server it
simply finds no calendars.
"""

from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..models.base import ApiModel

logger = logging.getLogger(__name__)

EDS = "org.gnome.evolution.dataserver"
SOURCE_MANAGER = "/org/gnome/evolution/dataserver/SourceManager"
CALENDAR_FACTORY = "/org/gnome/evolution/dataserver/CalendarFactory"


class DesktopCalendar(ApiModel):
    uid: str
    name: str
    account: str  # the online account (or "On This Computer")


@dataclass
class _Bus:
    """The few D-Bus calls we make, over one session-bus connection."""

    conn: object

    @classmethod
    def open(cls) -> _Bus:
        from jeepney.io.blocking import open_dbus_connection

        return cls(open_dbus_connection(bus="SESSION"))

    def call(self, bus_name: str, path: str, interface: str, method: str, sig="", args=()):
        from jeepney import DBusAddress, new_method_call

        addr = DBusAddress(path, bus_name=bus_name, interface=interface)
        reply = self.conn.send_and_get_reply(new_method_call(addr, method, sig, args), timeout=10)
        if reply.header.message_type.name == "error":
            raise RuntimeError(f"{method}: {reply.body}")
        return reply.body

    def names(self) -> list[str]:
        (names,) = self.call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames"
        )
        return names

    def close(self) -> None:
        self.conn.close()


def _service(bus: _Bus, prefix: str) -> str | None:
    """The versioned service name (e.g. ...Sources5, ...Calendar8) on this system."""
    found = sorted(n for n in bus.names() if n.startswith(f"{EDS}.{prefix}"))
    return found[-1] if found else None


def _value(v):
    return v[1] if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str) else v


def _sources(bus: _Bus) -> dict[str, configparser.ConfigParser]:
    service = _service(bus, "Sources")
    if service is None:
        return {}
    (objects,) = bus.call(
        service, SOURCE_MANAGER, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"
    )
    out = {}
    for interfaces in objects.values():
        props = interfaces.get(f"{EDS}.Source")
        if not props:
            continue
        data = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            data.read_string(_value(props.get("Data", ("s", ""))))
        except configparser.Error:
            continue
        out[_value(props["UID"])] = data
    return out


def list_calendars(bus: _Bus | None = None) -> list[DesktopCalendar]:
    """Enabled calendars, named with their account."""
    own = bus is None
    bus = bus or _Bus.open()
    try:
        sources = _sources(bus)
    finally:
        if own:
            bus.close()
    out = []
    for uid, data in sources.items():
        if not data.has_section("Calendar") or not data.has_section("Data Source"):
            continue
        if data["Calendar"].get("BackendName") == "contacts":  # birthdays, not meetings
            continue
        ds = data["Data Source"]
        if ds.get("Enabled", "true") != "true":
            continue
        parent = sources.get(ds.get("Parent", ""))
        account = (
            parent["Data Source"].get("DisplayName", "")
            if parent is not None and parent.has_section("Data Source")
            else ""
        )
        out.append(
            DesktopCalendar(
                uid=uid, name=ds.get("DisplayName", uid), account=account or "On This Computer"
            )
        )
    return sorted(out, key=lambda c: (c.account != "On This Computer", c.account, c.name))


def _ical_time(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def read_events(
    uids: list[str],
    start: datetime,
    end: datetime,
    bus: _Bus | None = None,
) -> str:
    """The events of these calendars (all when `uids` is empty) between start and end, as one
    VCALENDAR text with the time zones they use."""
    own = bus is None
    bus = bus or _Bus.open()
    try:
        wanted = uids or [c.uid for c in list_calendars(bus)]
        factory = _service(bus, "Calendar")
        if factory is None or not wanted:
            return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
        query = (
            f'(occur-in-time-range? (make-time "{_ical_time(start)}") '
            f'(make-time "{_ical_time(end)}"))'
        )
        events: list[str] = []
        zones: dict[str, str] = {}
        for uid in wanted:
            try:
                path, bus_name = bus.call(
                    factory, CALENDAR_FACTORY, f"{EDS}.CalendarFactory", "OpenCalendar", "s", (uid,)
                )
                iface = f"{EDS}.Calendar"
                bus.call(bus_name, path, iface, "Open")
                (objects,) = bus.call(bus_name, path, iface, "GetObjectList", "s", (query,))
                for obj in objects:
                    events.append(obj.strip())
                    for tzid in _tzids(obj):
                        if tzid not in zones:
                            try:
                                (zones[tzid],) = bus.call(
                                    bus_name, path, iface, "GetTimezone", "s", (tzid,)
                                )
                            except Exception:
                                zones[tzid] = ""  # icalendar knows Olson names anyway
                try:
                    bus.call(bus_name, path, iface, "Close")
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Could not read desktop calendar %s: %s", uid, e)
        body = [z.strip() for z in zones.values() if z.strip()] + events
        return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "\r\n".join(body) + "\r\nEND:VCALENDAR\r\n"
    finally:
        if own:
            bus.close()


def _tzids(ical: str) -> set[str]:
    out = set()
    for line in ical.splitlines():
        if ";TZID=" in line:
            out.add(line.split(";TZID=", 1)[1].split(":", 1)[0].split(";", 1)[0].strip('"'))
    return out


def window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """What a refresh reads: yesterday to two weeks ahead."""
    now = (now or datetime.now()).astimezone()
    return now - timedelta(days=1), now + timedelta(days=14)
