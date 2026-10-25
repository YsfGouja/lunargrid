# LunarGrid

LunarGrid helps lunar mission planners check site sunlight, base battery forecasts, task power use, deliveries, and rover route plans.

## Problem and users

The team scenario is a future with several Moon work sites connected by battery-powered rovers. As crews add habitats, routes, and energy-heavy work, a planner must know whether a rover can reach a site, finish its assigned tasks, and keep enough reserve power. Mission planners and rover/power operators are the users. Today, a spreadsheet can list batteries and schedules, but it is difficult to compare changing Sun conditions, routes, task loads, and shared plan edits together. LunarGrid puts those checks into one plan that a team can run, explain, save, and share.

## Run locally

Use Python 3.11 or newer. Install the packages in `requirements.txt`, then set `DATABASE_URL` and `SECRET_KEY` in a local `.env` file. For a quick local database, use `DATABASE_URL=sqlite:///./lunagrid.db`.

```powershell
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8081
```

Open [http://127.0.0.1:8081/app](http://127.0.0.1:8081/app). The first registered account becomes the admin. Admins can load the judge demo data; loading it again resets the named judge scenarios and demo clock, without deleting other data. API documentation is at `/docs`.

## Simulation flow

1. Add Moon sites with latitude and longitude, or load the admin demo.
2. Select a start, destination, optional waypoints, tasks, rover starting charge, and cargo weight in **Simulation**.
3. Run the rover comparison. The battery chart shows charge over travel and work; failed routes say where the plan runs short.
4. Save a plan to keep its route, task order, battery starting values, and cargo. Share it with another registered user by email.
5. Use **Advance base by 1 hour** to apply the selected site's next battery forecast to its saved charge and simulation clock.

## Judge demo tour

1. Sign in as the first account (admin), load the demo, and select **Demo Base** in the site selector.
2. On **Dashboard**, show the live Sun state, battery forecast, site power budget, and task/equipment delivery conflicts.
3. Open **Tasks**, **Equipment**, and **Deliveries** to show planned, in-progress, completed, cancelled, available, offline, in-transit, delayed, arrived, and installed examples.
4. Open **Simulation** and use **Judge demo shortcuts**. Run **Quick trip** for the likely success, **Heavy cargo** to compare rover payload limits, and **Drill run** to compare energy use.
5. Run **Waypoint + return**, **Low battery**, **Multiple tasks**, and **Saved sunlight windows** to show route length, order suggestions, and manual-light fallback. Use **Open relay outage dashboard** to show the separate site's low battery and predicted shortage.
6. Load **Judge Demo — Quick ridge delivery** under saved plans. To show collaboration, create a second account, then share the plan with that account's email.
7. Use **Advance base by 1 hour** and return to **Batteries** or **Dashboard** to show the changed saved charge. The demo seed is safe to run again.

## Sun and rover data

- Live Sun elevation and azimuth come from the public [JPL Horizons API](https://ssd-api.jpl.nasa.gov/doc/horizons.html), calculated for the Moon surface coordinates. If it cannot be reached, LunarGrid uses that site's saved sunlight windows and labels the fallback.
- Solar generation is an estimate: the panel is treated as level and output scales with the Sun's elevation. Local hills, terrain, panel direction, dust, temperature, and hardware limits are not modeled.
- Cargo is checked against each rover's stated payload limit, but cargo weight does not change the energy-use estimate.
- The Apollo Lunar Roving Vehicle profile uses historical NASA battery, payload, and speed information. Its per-kilometre energy use is a derived estimate, not a measured drive record. The scout and cargo rover profiles are fictional planning scenarios, not real vehicle specifications. See [NASA's LRV reference](https://nssdc.gsfc.nasa.gov/planetary/lunar/apollo_lrv.html) and [Apollo 16 press kit](https://www.nasa.gov/wp-content/uploads/static/apollo50th/pdf/A16_PressKit.pdf).
- Routes use straight-line distances between entered sites and waypoints. The simulation does not select safe paths around terrain or obstacles.

## Check the model

```powershell
python -m unittest -v test_mission.py
```

The tests cover lunar route distance, battery depletion, sunlight charging, rover payload limits, task ordering, and reading JPL Sun rows.

## Container

Build and run with a persistent database URL and a private signing key:

```powershell
docker build -t lunargrid .
docker run --rm -p 8081:8081 -e DATABASE_URL="sqlite:////data/lunagrid.db" -e SECRET_KEY="replace-with-a-long-random-secret" -v lunargrid-data:/data lunargrid
```

For a shared hosted service, point `DATABASE_URL` at managed PostgreSQL, use a secret key stored by the host, and restrict the current development CORS setting to the deployed app's origin. The container does not create a hosted database or provision accounts.
