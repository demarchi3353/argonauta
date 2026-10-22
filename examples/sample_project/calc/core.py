"""Tiny calculator used as Argonauta sample data. Safe to delete."""


def add(a: float, b: float) -> float:
    return a + b


def subtract(a: float, b: float) -> float:
    return a - b


def multiply(a: float, b: float) -> float:
    return a * b


def divide(a: float, b: float) -> float:
    if b == 0:
        raise ZeroDivisionError("cannot divide by zero")
    return a / b


def power(base: float, exponent: int) -> float:
    result = 1.0
    for _ in range(exponent):
        result *= base
    return result
