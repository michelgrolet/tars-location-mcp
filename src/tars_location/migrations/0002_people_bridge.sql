-- Optional. Run this only if a people graph lives in the same database.
--
-- "Who was I with in Lisbon" is the question a location archive cannot answer on its own,
-- and the one people actually ask. It needs a table of people, which is a different product
-- with a different lifecycle, so it is a bridge rather than a dependency: the core schema
-- never references `people`, and everything here fails loudly and immediately if it is
-- absent rather than half-installing.
--
-- Built against people-memory (https://github.com/michelgrolet/people-memory-mcp), which
-- provides `people (id, full_name, current_org, current_role)`. Any table with those columns
-- works. Nothing else about that schema is touched or assumed.
--
--     psql "$LOCATION_DATABASE_URL" -f migrations/0002_people_bridge.sql

begin;

do $$
begin
  if to_regclass('public.people') is null then
    raise exception
      'no `people` table in this database. This migration is the optional bridge to a people '
      'graph; skip it, or install one first. The location schema works without it.';
  end if;
end $$;

-- No foreign key to `people` on purpose. A hard reference would make the people graph
-- undroppable and would tie the two products' migrations together forever, which is exactly
-- what keeping them in separate repos is meant to avoid. The join is checked at read time.
create table if not exists location_trip_people (
  trip_id    bigint not null references location_trips (id) on delete cascade,
  person_id  bigint not null,
  role       text not null default 'with',
  note       text not null default '',
  added_at   timestamptz not null default now(),
  primary key (trip_id, person_id)
);
create index if not exists location_trip_people_person on location_trip_people (person_id);

-- Read it from the trip's side. `current_role` is a reserved word in Postgres and reading it
-- back unquoted returns the database role for every row instead of the person's job, silently,
-- so it is renamed here and never surfaces under that name again.
create or replace view location_v_trip_people as
select t.id as trip_id, t.slug, t.name as trip_name, t.started_at, t.ended_at,
       p.id as person_id, p.full_name, p.current_org, p."current_role" as job_title,
       tp.role, tp.note
from location_trips t
join location_trip_people tp on tp.trip_id = t.id
join people p on p.id = tp.person_id;

-- And from the person's side, which is the direction "where have we been together" reads.
create or replace view people_v_trips as
select p.id as person_id, p.full_name, t.id as trip_id, t.slug, t.name as trip_name,
       t.started_at, t.ended_at, t.nights, t.countries, t.primary_country, tp.role, tp.note
from people p
join location_trip_people tp on tp.person_id = p.id
join location_trips t on t.id = tp.trip_id;

commit;
