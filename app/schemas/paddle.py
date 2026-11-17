from typing import Annotated
from fastapi import Path


PaddleOrderId = Annotated[str, Path(pattern=r"^txn_[a-z\d]{26}$", description="Paddle transaction ID (txn_…)")]
