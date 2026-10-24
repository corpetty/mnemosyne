"""Contacts: other people whose computers this library shares meetings with
(services/contacts.py). A card is what a person hands over when pairing, signed with their
person key (services/person.py)."""

from pydantic import BaseModel

from .base import ApiModel


class CardMachine(BaseModel):
    name: str
    ticket: str  # the machine's iroh endpoint ticket (services/link.py)


class Card(BaseModel):
    person: str  # the public key, 64 hex characters
    name: str
    machines: list[CardMachine]
    at: str  # ISO 8601, UTC; a newer card replaces an older one
    sig: str  # the person's signature of the canonical card without this field


class ContactPullInfo(ApiModel):
    """How pulling what this contact shares went last (services/share_pull.py)."""

    state: str  # idle | pulling | ok | unreachable | skew | error | gone (they removed us)
    at: str | None = None  # when the last round ended, ISO 8601 UTC
    error: str | None = None


class ContactInfo(ApiModel):
    person: str
    pid: str  # "p:" + the first 16 hex characters of the key
    name: str  # what to show: my_name, else the card's name
    card_name: str
    fingerprint: str
    machines: list[str]  # names only
    created_at: str
    updated_at: str
    pull: ContactPullInfo | None = None  # None: not pulled from since this start


class ContactsMe(ApiModel):
    person: str
    pid: str
    name: str  # what others are shown (the card's name)
    fingerprint: str


class ContactsResponse(ApiModel):
    me: ContactsMe
    contacts: list[ContactInfo]


class ContactPairRequest(BaseModel):
    invite: str  # <ticket>#<code>, from the other person's "Add a contact" dialog


class ContactRename(BaseModel):
    name: str = ""  # empty: back to the name on their card
