from events import IssuedItem

from pydantic import BaseModel, Field

from typing import Literal


class ClaimRequest(BaseModel):
    """Body of a license claim: never in the URL, never logged"""
    # The secret's 32 random bytes as 64 hex characters (the browser hashes the bytes, see claims.claim_matches)
    claim_secret: str = Field(pattern=r"^[0-9a-fA-F]{64}$", repr=False)


class ClaimResponse(BaseModel):
    """
    licenses: the order's items, keys included (one per unit, none for a trial or an undelivered item)
    forbidden: the secret doesn't match the order, or the order can't be claimed
    pending: the order's licenses weren't issued in time
    """
    status: Literal["licenses", "forbidden", "pending"]
    items: list[IssuedItem] | None = None
