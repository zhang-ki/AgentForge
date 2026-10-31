"""Refrigerated transport routing."""

from .weather import score_weather_risk


def plan_route(origin: str, destination: str, weather: dict) -> dict:
    """Plan a lane and attach the current static weather risk."""
    risk = score_weather_risk(weather["wind_kph"], weather["precipitation_mm"])
    return {"origin": origin, "destination": destination, "weather_risk": risk}
