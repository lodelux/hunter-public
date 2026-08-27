"""Fetch a few live jobs with JobSpy and print the useful fields."""

import argparse

from jobspy import scrape_jobs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="linkedin")
    parser.add_argument("--term", default="software engineer")
    parser.add_argument("--location", default="Milan, Lombardy, Italy")
    parser.add_argument("--results", type=int, default=5)
    args = parser.parse_args()

    jobs = scrape_jobs(
        site_name=[args.site],
        search_term=args.term,
        location=args.location,
        results_wanted=args.results,
        country_indeed="Italy",
    )

    if jobs.empty:
        raise SystemExit("JobSpy returned no jobs.")

    columns = [
        column
        for column in ("title", "company", "location", "date_posted", "job_url")
        if column in jobs.columns
    ]
    print(f"\nJobSpy returned {len(jobs)} jobs:\n")
    print(jobs[columns].to_string(index=False))


if __name__ == "__main__":
    main()
