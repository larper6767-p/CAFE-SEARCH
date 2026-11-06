-- Enable PostGIS in the dedicated 'extensions' schema, never in public.
-- (Supabase docs: PostGIS creates spatial_ref_sys in the schema it is installed
-- in; if installed in public, that table is exposed through the Data API.)
create extension if not exists postgis with schema "extensions";
