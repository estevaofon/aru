"""Funções matemáticas utilitárias."""


def add(a, b):
    """Retorna a soma de dois números."""
    return a + b


def subtract(a, b):
    """Retorna a subtração de dois números."""
    return a - b


def multiply(a, b):
    """Retorna o produto de dois números."""
    return a * b


def divide(a, b):
    """Retorna a divisão de dois números.

    Levanta ValueError se b for zero.
    """
    if b == 0:
        raise ValueError("Não é possível dividir por zero.")
    return a / b