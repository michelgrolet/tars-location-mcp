---
name: where-was-i
description: Read the location archive before answering anything about where the user has been, when, for how long, how far, or who with. Use for "when was I in X", "how long were we in Y", "which cities last spring", "how many times have I been to Z", "what did I do on the 8th", "how far did I fly", "where do I live", and for any date or place the user names as if you already knew it.
---

# Where was I

The user has an archive of their own past positions. It is the only thing in the room that knows the answer, and it is cheap to ask. Read it before answering, not after being corrected.

## Pick the tool that matches the question

| The question | The tool |
|---|---|
| where am I, is this fix current | `current_location` |
| what did I do on a date | `day` |
| every stop over a window, in order | `stays` |
| which cities, how long in each, how many separate times | `cities_visited` |
| the same by country | `countries_visited` |
| where do I spend my time, what was that address | `top_places` |
| what trips have I taken | `trips`, then `trip` for one in full |
| kilometres, flights, days away | `travel_stats` |
| highest, farthest, longest flight, most cities in a day | `records` |
| where have I lived | `home` |
| who was I with | `who_was_there` |
| anything the above does not shape | `location_sql`, read-only |

`stays`, `cities_visited`, `countries_visited`, `travel_stats` and `top_places` all take either `period` (`today`, `yesterday`, `this_week`, `last_week`, `this_month`, `last_month`, `this_year`, `last_year`, `last_N_days`, `all`) or an explicit `since`/`until` pair. Prefer the explicit pair whenever the user named real dates.

## Empty is not the same as never

The archive is assembled from an export plus a live feed, so it has holes: the months between the day the export was taken and the day the tracker was installed, a phone that was replaced, an app that was uninstalled.

**When a tool comes back empty, call `location_coverage` before saying anything.** It reports every gap of two weeks or more, measured from the data. If the window the user asked about sits inside a gap, the answer is "nothing was recorded then", never "you were not there". Those two sentences are not interchangeable and only one of them is honest.

Every windowed answer already carries the period and timezone it used. Keep them when you report: an answer that does not say what it covers is not an answer.

## Report what was measured

Durations, distances and dates come from the archive. Do not round a stay into a nicer number, do not interpolate between two stays to fill an afternoon, and do not convert "no journey recorded" into "they drove". If the user asks something the archive cannot answer, say which tool you tried and what it held.

Do not volunteer someone's location to anyone but the user.
