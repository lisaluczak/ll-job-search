# Job Facts

A small website that searches for Seattle-area nutrition, public health, research, and food-regulatory roles twice a day and lists them, best match first. It runs free on GitHub: GitHub Actions does the searching, and GitHub Pages hosts the page.

Setup takes about 15 minutes and is easiest on a computer. After that, everything works from a phone.

## What it searches

| Source | What it covers | Needs a key? |
|---|---|---|
| Adzuna | Pulls from many job boards. The broadest source, so worth setting up. | Free key |
| USAJobs | Federal roles in Seattle (VA Puget Sound dietitians, FDA, etc.) | Free key |
| Gates Foundation | Their Workday careers site | No |
| Amazon & Whole Foods | amazon.jobs, nutrition and labeling roles | No |
| Fred Hutch | Their iCIMS careers site | No |
| UW Public Health job board | sph.washington.edu/careers/jobs | No |
| King County, WA State, City of Seattle | GovernmentJobs.com feeds (experimental) | No |

LinkedIn, Indeed, and Google Jobs don't allow automated searching, so the page has one-tap saved searches for those instead.

Every listing gets a match score from keywords in `config.json` (title words count most). Roles in far-off places, senior leadership roles, and unrelated jobs are filtered or hidden.

## Setup

**1. Make the repository.** Sign in to github.com, click **New repository**, name it something neutral like `job-radar`, keep it **Public**, and create it.

> GitHub Pages sites are public. Nothing in these files names anyone, so keep it that way. (Private repos can use Pages only on a paid GitHub plan.)

**2. Upload the files.** On the new repo page, click **uploading an existing file** and drag in everything from the unzipped folder.

The `.github` folder is hidden on most computers and often gets skipped. Check that it arrived. If it didn't: **Add file → Create new file**, type `.github/workflows/update.yml` as the name, paste in the contents of that file, and commit.

**3. Allow the workflow to save results.** Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save.

**4. Turn on the website.** Settings → Pages → Source: **Deploy from a branch** → Branch: **main**, folder **/ (root)** → Save. After a minute the page lives at `https://YOUR-USERNAME.github.io/job-radar/`.

**5. Add the free API keys (recommended).**

- Adzuna: sign up at https://developer.adzuna.com and copy the **Application ID** and **Application Key**.
- USAJobs: request a key at https://developer.usajobs.gov (it's emailed to you).

Then in the repo: Settings → Secrets and variables → Actions → **New repository secret**, once for each:

| Name | Value |
|---|---|
| `ADZUNA_APP_ID` | your Adzuna application ID |
| `ADZUNA_APP_KEY` | your Adzuna application key |
| `USAJOBS_API_KEY` | your USAJobs key |
| `USAJOBS_EMAIL` | the email you registered with USAJobs |

Keys stay private in GitHub's secret store; they never appear on the page. Without keys, those two sources show as "Off" and the rest still run.

**6. Run the first search.** Actions tab → **Update job listings** → **Run workflow**. It takes about two minutes. Then open the page.

**7. Put it on the home screen.** Open the page on the phone, then Share → **Add to Home Screen**.

## Using it

- Searches run on their own around 7 AM and 5 PM Pacific.
- For a fresh search right now, tap **Run a fresh search** on the page, then **Run workflow** on GitHub (you need to be signed in to GitHub). Reload the page two minutes later.
- Tap any line in the facts panel to filter: new since your last visit, saved, or by type.
- **Save** and **Hide** are remembered on that device.
- **Where these roles come from** at the bottom shows which sources worked on the last run.

## Changing what it looks for

Everything lives in `config.json`. Edit it right on GitHub (open the file, click the pencil), then run the workflow.

- `search_terms`: what gets searched on each site.
- `scoring.title` / `scoring.text`: words that raise the match score. `scoring.penalties`: words that lower it. A word ending in `*` matches anything starting with it (`epidemiolog*`).
- `scoring.min_score`: raise it for a shorter, stricter list.
- `location.allowed_locations`: places that count as close enough.
- `sources`: turn sources on or off. To add any employer whose careers site address contains `myworkdayjobs.com`, copy the Gates entry and paste in that employer's URL.
- `quick_links`: the saved-search links at the bottom of the page.

## If something stops working

Employer sites change their pages now and then. When that happens, that one source shows **Not working** at the bottom of the page with the error, and the rest keep going. Roles from a failing source stay listed for up to 10 days. Fix it by updating or turning off that source in `config.json`.

The GovernmentJobs.com feeds are the most likely to need attention. If they show Not working, use the King County and WA State quick links instead.

GitHub pauses scheduled workflows in repos with no activity for 60 days. The twice-daily update counts as activity, so this only matters if the workflow has been failing for two months.

To run it on your own computer: `pip install -r requirements.txt`, then `python scripts/fetch_jobs.py`, then `python -m http.server` and open http://localhost:8000.
