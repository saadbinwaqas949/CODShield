"""Check v2 risk scores against later completed COD outcomes (read-only)."""
from db import connect


# Compare saved risk scores with completed delivery outcomes.
def main():
    conn = connect()
    try:
        with conn.cursor() as cur:
            # Pair each completed order with its latest v2 score to avoid duplicate assessments.
            cur.execute(
                "select ra.score, (o.status::text in ('refused','returned'))::int as failed "
                "from orders o join lateral ("
                "  select score from risk_assessments "
                "  where order_id=o.id and model_version='rules-v2-credibility' "
                "  order by created_at desc limit 1"
                ") ra on true "
                "where o.status::text in ('delivered','refused','returned') "
                "order by o.updated_at"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("No completed orders scored by rules-v2-credibility yet.")
        return

    # The Brier score is mean squared probability error, where refused/returned is the positive class.
    brier = sum((float(score) - int(failed)) ** 2 for score, failed in rows) / len(rows)
    print(f"Completed orders: {len(rows)} | Brier score (lower is better): {brier:.4f}")
    print("Score band | Orders | Mean predicted | Observed refused/returned")
    for band in range(5):
        low, high = band / 5, (band + 1) / 5
        group = [(float(s), int(y)) for s, y in rows
                 if low <= float(s) < high or (band == 4 and float(s) == 1)]
        if group:
            predicted = sum(s for s, _ in group) / len(group)
            observed = sum(y for _, y in group) / len(group)
            print(f"{low:.1f}-{high:.1f}   | {len(group):6d} | {predicted:14.1%} | {observed:23.1%}")
    print("Treat this as a diagnostic, not proof of calibration; compare on a future time window.")


if __name__ == "__main__":
    main()
