# PropertyHunter-ai
        AI platform for finding and analyzing real estate investment opportunities

## Supabase integration

This project can persist analyzed properties to Supabase automatically.

### 1) Configure environment variables

Add these variables to `.env`:

```
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
```

`OPENAI_API_KEY` is still required for analysis.

### 2) Apply database schema

Run the SQL in `supabase/schema.sql` in the Supabase SQL editor.

Tables created:

- `properties`
- `analyses`
- `transactions`
- `permits`
- `energy_labels`

### 3) Automatic storage behavior

Every successful analysis from both app flows is stored automatically:

- URL analysis
- Manual text analysis

If Supabase is not configured, the app keeps working without persistence.
