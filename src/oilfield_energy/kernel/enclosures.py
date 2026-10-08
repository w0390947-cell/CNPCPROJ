"""Outward enclosures of exact arithmetic on declared binary floating inputs."""

from fractions import Fraction
from math import inf, isqrt, nextafter


def lower(value: Fraction) -> float:
    result = float(value)
    return nextafter(result, -inf) if Fraction(result) > value else result


def upper(value: Fraction) -> float:
    result = float(value)
    return nextafter(result, inf) if Fraction(result) < value else result


def sqrt_bounds(value: Fraction) -> tuple[Fraction, Fraction]:
    """Enclose a nonnegative square root with exact decimal rationals."""
    if value < 0:
        raise ValueError("negative squared magnitude")
    scale = 10**24
    n = isqrt(value.numerator * scale**2 // value.denominator)
    low = Fraction(n, scale)
    return low, low if low * low == value else Fraction(n + 1, scale)


def magnitude(real: Fraction, imag: Fraction) -> tuple[Fraction, Fraction]:
    return sqrt_bounds(real * real + imag * imag)
