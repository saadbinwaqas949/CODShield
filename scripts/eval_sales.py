"""Run a paid, manual evaluation of sales conversations against the configured model."""
import os
import re

from db import connect
from handlers import ROMAN_PRODUCT_ALIASES, SALES_PROMPT, find_product
from llm import ask_json


CASES = [
    ("Roman Urdu", "Mujhe Cotton Shirt chahiye, aik, Ali, House 5 DHA Lahore", "Cotton Shirt", 1, "haan"),
    ("Roman Urdu", "Black Cap aik chahiye, Sara, House 12 Gulberg Lahore", "Black Cap", 1, "jee"),
    ("Roman Urdu", "Running Shoes do, Ahmed, House 8 Clifton Karachi", "Running Shoes", 2, "bilkul"),
    ("Roman Urdu", "Kaali topi 3 chahiye, Sana, House 4 F-7 Islamabad", "Black Cap", 3, "theek hai"),
    ("Roman Urdu", "Kaatan shirt 2, Usman, House 9 Saddar Rawalpindi", "Cotton Shirt", 2, "haan confirm"),
    ("English", "I want one Cotton Shirt. Name: Ali. Address: House 5 DHA Lahore", "Cotton Shirt", 1, "yes"),
    ("English", "Please order two Running Shoes for Sara, 12 Gulberg Lahore", "Running Shoes", 2, "yes please"),
    ("English", "One Black Cap for Ahmed, House 8 Clifton Karachi", "Black Cap", 1, "confirm"),
    ("English", "Three Cotton Shirts, Sana, House 4 F-7 Islamabad", "Cotton Shirt", 3, "go ahead"),
    ("English", "I need two Black Caps, Usman, House 9 Saddar Rawalpindi", "Black Cap", 2, "yes"),
    ("Urdu script", "\u0645\u062c\u06be\u06d2 \u06a9\u0627\u0679\u0646 \u0634\u0631\u0679 \u0686\u0627\u06c1\u06cc\u06d2\u060c \u0627\u06cc\u06a9\u060c \u0639\u0644\u06cc\u060c \u06af\u06be\u0631 5 \u0688\u06cc \u0627\u06cc\u0686 \u0627\u06cc \u0644\u0627\u06c1\u0648\u0631", "Cotton Shirt", 1, "\u06c1\u0627\u06ba"),
    ("Urdu script", "\u0633\u06cc\u0627\u06c1 \u0679\u0648\u067e\u06cc \u0627\u06cc\u06a9 \u0686\u0627\u06c1\u06cc\u06d2\u060c \u0633\u0627\u0631\u06c1\u060c \u06af\u06be\u0631 12 \u06af\u0644\u0628\u0631\u06af \u0644\u0627\u06c1\u0648\u0631", "Black Cap", 1, "\u062c\u06cc"),
    ("Urdu script", "\u0631\u0646\u0646\u06af \u0634\u0648\u0632 \u062f\u0648 \u0686\u0627\u06c1\u06cc\u06ba\u060c \u0627\u062d\u0645\u062f\u060c \u06af\u06be\u0631 8 \u06a9\u0644\u0641\u0679\u0646 \u06a9\u0631\u0627\u0686\u06cc", "Running Shoes", 2, "\u0628\u0627\u0644\u06a9\u0644"),
    ("Urdu script", "\u06a9\u0627\u0679\u0646 \u0634\u0631\u0679 \u062a\u06cc\u0646 \u0686\u0627\u06c1\u06cc\u06ba\u060c \u0633\u0646\u0627\u060c \u06af\u06be\u0631 4 \u0627\u06cc\u0641 7 \u0627\u0633\u0644\u0627\u0645 \u0622\u0628\u0627\u062f", "Cotton Shirt", 3, "\u0679\u06be\u06cc\u06a9 \u06c1\u06d2"),
    ("Urdu script", "\u06a9\u0627\u0644\u06cc \u0679\u0648\u067e\u06cc \u062f\u0648 \u0686\u0627\u06c1\u06cc\u06ba\u060c \u0639\u062b\u0645\u0627\u0646\u060c \u06af\u06be\u0631 9 \u0635\u062f\u0631 \u0631\u0627\u0648\u0644\u067e\u0646\u0688\u06cc", "Black Cap", 2, "\u06c1\u0627\u06ba"),
]
# These scripted cases cover order extraction, language matching, and explicit confirmation.


# Check whether a reply matches the expected language style.
def _reply_matches_language(text, language):
    has_urdu = bool(re.search(r"[\u0600-\u06ff]", text or ""))
    if language == "Urdu script":
        return has_urdu
    if has_urdu:
        return False
    if language == "English":
        return bool(re.search(r"[A-Za-z]", text or ""))
    return any(word in (text or "").lower().split() for word in
               ("aap", "mujhe", "hai", "hain", "chahiye", "jee", "haan", "maazrat"))


# Run scripted conversations against the configured Gemini model.
def main():
    missing = [name for name in ("GEMINI_API_KEY",) if not os.getenv(name)]
    if missing:
        raise SystemExit("Set these model settings before running the paid evaluation: "
                         + ", ".join(missing))
    conn = connect()
    cur = conn.cursor()
    cur.execute("select id,name,name_ur,price,stock from products "
                "where seller_id=(select id from sellers where name=%s)", ("Demo Store",))
    products = [dict(id=r[0], name=r[1], name_ur=r[2], price=float(r[3]), stock=r[4])
                for r in cur.fetchall()]
    if not products:
        raise SystemExit("No Demo Store products found. Run seed.py first.")
    catalog = "\n".join(
        f"- {p['name']} / {p.get('name_ur') or ''} / "
        f"{', '.join(ROMAN_PRODUCT_ALIASES.get(p['name'], ())) or p['name']}: "
        f"Rs {p['price']:.0f} (stock {p['stock']})" for p in products)
    system = SALES_PROMPT.replace("{shop}", "Demo Store").replace("{catalog}", catalog)

    print("# | Language | Before yes | After yes | Result")
    print("--|----------|------------|-----------|-------")
    # Compare the initial proposal with the model response after a language-matched confirmation.
    for number, (language, first_message, product_name, qty, yes) in enumerate(CASES, 1):
        history = [{"role": "user", "content": first_message}]
        before = ask_json(system, history, cur=cur).get("result")
        order_before = before.get("order") if before else "invalid response"
        reply_before = before.get("reply", "") if before else ""
        confirm_prompt = {
            "Roman Urdu": "Aap ka order yeh hai. Barah-e-karam confirm kar dein.",
            "English": "Here is your order. Please confirm.",
            "Urdu script": "\u0622\u067e \u06a9\u0627 \u0622\u0631\u0688\u0631 \u06cc\u06c1 \u06c1\u06d2\u06d4 \u0628\u0631\u0627\u06c1 \u06a9\u0631\u0645 \u062a\u0635\u062f\u06cc\u0642 \u06a9\u0631\u06cc\u06ba\u06d4",
        }[language]
        history.extend([
            {"role": "assistant", "content": confirm_prompt + " " + product_name},
            {"role": "user", "content": yes},
        ])
        after = ask_json(system, history, cur=cur).get("result")
        order_after = after.get("order") if after else None
        reply_after = after.get("reply", "") if after else ""
        # A pass requires no premature order, a valid confirmed proposal, correct language, and item.
        passed = (order_before is None and isinstance(order_after, dict)
                  and _reply_matches_language(reply_before, language)
                  and _reply_matches_language(reply_after, language))
        if passed:
            items = order_after.get("items") or []
            passed = any(
                item.get("qty") == qty
                and (matched := find_product(products, item.get("product_name"))) is not None
                and matched["name"] == product_name
                for item in items if isinstance(item, dict)
            )
        status = "PASS" if passed else "FAIL"
        print(f"{number:2} | {language:10} | {order_before is None!s:10} | {isinstance(order_after, dict)!s:9} | {status}")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
