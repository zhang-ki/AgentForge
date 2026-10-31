"""Static route weather scoring."""


def score_weather_risk(wind_kph: float, precipitation_mm: float) -> float:
    """Compute a deterministic weather risk score for route planning."""
    return round(wind_kph / 100.0 + precipitation_mm / 50.0, 3)
