"""Column types that make two invariants true at the storage boundary.

``ExactDecimal`` - no money value passes through a float, on any backend.
``UtcDateTime``  - no naive datetime is ever stored.

Both raise rather than coerce. A silent conversion here is exactly the kind of
defect this application exists to detect in other people's systems.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Dialect, String, TypeDecorator


class ExactDecimal(TypeDecorator[Decimal]):
    """Stores a Decimal as fixed-width TEXT, exact on SQLite and Postgres alike.

    SQLAlchemy's ``Numeric`` degrades to float on SQLite. In an application whose
    entire purpose is arithmetic about money, that is disqualifying, so values
    are stored as text normalised to a fixed scale.

    The fixed width makes plain string ordering agree with numeric ordering for
    non-negative values, which is what the worklist sorts on. Negative values
    sort by magnitude in reverse; nothing orders on a signed money column, and
    ordering on one would be a defect.
    """

    impl = String
    cache_ok = True

    INT_DIGITS = 20

    def __init__(self, scale: int = 12, **kwargs: Any) -> None:
        # Alembic re-constructs column types from the rendered repr and passes
        # the impl's own kwargs back in. Width is derived from scale, so any
        # length it hands us is discarded rather than fought with.
        kwargs.pop("length", None)
        self.scale = scale
        super().__init__(length=self.INT_DIGITS + scale + 2, **kwargs)

    def __repr__(self) -> str:
        # Drives how autogenerate writes this type into a migration.
        return f"{type(self).__name__}(scale={self.scale})"

    @property
    def _width(self) -> int:
        return self.INT_DIGITS + self.scale + 1

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError(
                "refusing to store a float in a money column; pass a Decimal "
                "(see CLAUDE.md invariant 1)"
            )
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and -exponent > self.scale:
            raise ValueError(
                f"value {value} has more than {self.scale} decimal places; "
                "storing it would round, and values are stored at the precision received"
            )
        quantized = value.quantize(Decimal(1).scaleb(-self.scale))
        sign = "-" if quantized < 0 else "0"
        return sign + format(abs(quantized), f"0{self._width}.{self.scale}f")

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        magnitude = Decimal(value[1:])
        return -magnitude if value[0] == "-" else magnitude


class UtcDateTime(TypeDecorator[datetime]):
    """Stores a timezone-aware datetime as UTC, and refuses naive ones."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            raise ValueError(
                "refusing to store a naive datetime; every timestamp is "
                "timezone-aware UTC (see CLAUDE.md invariant 6)"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        stored: datetime = value
        if stored.tzinfo is None:
            return stored.replace(tzinfo=UTC)
        return stored.astimezone(UTC)


Money = ExactDecimal(scale=12)
