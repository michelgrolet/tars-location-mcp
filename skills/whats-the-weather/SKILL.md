---
name: whats-the-weather
description: Answer any weather question through the location archive, which already knows where the user is. Use for "will it rain today", "do I need an umbrella", "what is it like outside", "how hot will it be", "what is the weekend looking like", "is it going to rain on the trip", "was it raining when we were in X", and for any weather question that names no place at all.
---

# What's the weather

The user has an archive of their own positions, so a weather question needs no coordinates and no city. Read the position, then the weather. Never ask them where they are.

## Pick the tool that matches the question

| The question | The tool |
|---|---|
| will it rain, should I take an umbrella, what are the chances | `will_it_rain` |
| what is it like outside right now | `weather_now` |
| the next few days, the weekend, the trip | `weather_forecast` |
| how reliable is this, why do two apps disagree | `weather_models` |
| what was the weather that day, on that trip | `weather_history` |

All five default to where the user is. Pass `place` for somewhere else, and `lat`/`lon` only when you already have coordinates. `will_it_rain` covers the rest of today by default; pass `date` for a whole day up to about four days out, or `hours` for the next N.

## Anything phrased as a chance goes through `will_it_rain`

`weather_now` and `weather_forecast` carry a single model's own chance of rain. `will_it_rain` counts about 120 ensemble members from three centres. When the user asks how likely something is, the counted number is the one to report.

Report the percentage **and** the verdict word next to it: "62 %, likely" says more than either alone. When the answer says the centres disagree, say that too, in the same breath — a 40 % they all agree on and a 40 % that is ECMWF at 5 % against NOAA at 75 % are not the same forecast, and the second one is a reason to check again later.

The dry window is usually the real answer. "It rains this afternoon but you have until 14:00" is what the question was about.

## Say which position was used

Every answer carries a `where` block with the coordinates and how they were decided. When it says the fix is stale, or that a past-weather question fell back to the current position because nothing was recorded that day, that belongs in the reply. A forecast for the wrong town is indistinguishable from a wrong forecast.

Do not convert the numbers. If the answer is in °C and mm, report °C and mm.
