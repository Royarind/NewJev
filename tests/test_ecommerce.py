"""Unit tests for the Agentic Ecommerce & Shopping Module in NewJev."""

from app.ecommerce.bridge import (
    is_ecommerce_goal,
    extract_ecommerce_details,
    load_default_profile,
)
from app.tools.registry import get_registry
from app.tools.router import ActionRouter


def test_is_ecommerce_goal_detection():
    # Ecommerce price comparison queries
    assert is_ecommerce_goal("Can you give me the prices of Samsung S25 ultra Titanium Black 256 gb in both Amazon.in and Flipkart? And suggest where should I buy it from") is True
    assert is_ecommerce_goal("can you find out where can I find the cheapest iphone 17 pro max and what would be the price") is True
    assert is_ecommerce_goal("Find noise cancelling headphones on Google Shopping under 15000") is True

    # Checkout / buying queries
    assert is_ecommerce_goal("Go to kyliecosmetics.com and buy Cosmic 100ml perfume") is True
    assert is_ecommerce_goal("Add Nike running shoes size 10 to cart") is True
    assert is_ecommerce_goal("Order iPhone 16 Pro Max from Apple") is True

    # Non-ecommerce queries
    assert is_ecommerce_goal("Open Notepad and write a summary") is False
    assert is_ecommerce_goal("Go to the taskbar, click on the speaker icon and increase the volume") is False
    assert is_ecommerce_goal("Give me the latest news on The times of India") is False


def test_extract_ecommerce_details():
    res = extract_ecommerce_details("Go to https://example.com/product and buy size 10 color blue")
    assert res["url"] == "https://example.com/product"
    assert res["variants"].get("size") == "10"
    assert res["is_checkout"] is True

    res2 = extract_ecommerce_details("Can you give me the prices of Samsung S25 ultra 256 gb in both Amazon.in and Flipkart?")
    assert "256 gb" in res2["variants"].get("capacity", "").lower()
    assert res2["is_checkout"] is False


def test_ecommerce_tools_registered():
    reg = get_registry()
    shop_tool = reg.get("ecommerce_shop")
    checkout_tool = reg.get("ecommerce_checkout")

    assert shop_tool is not None
    assert checkout_tool is not None
    assert shop_tool.is_available()[0] is True
    assert checkout_tool.is_available()[0] is True


def test_action_router_ecommerce_routing():
    router = ActionRouter()

    # Shopping / price comparison should route to ecommerce_shop
    decision = router.route("Can you give me the prices of Samsung S25 ultra in both Amazon.in and Flipkart?")
    assert decision.route_type == "direct_tool"
    assert decision.recommended_tool == "ecommerce_shop"

    # Direct checkout should route to ecommerce_checkout
    decision2 = router.route("buy from https://example.com/shoes size 10")
    assert decision2.route_type == "direct_tool"
    assert decision2.recommended_tool == "ecommerce_checkout"


def test_load_default_profile():
    prof = load_default_profile()
    assert prof is not None
    assert hasattr(prof, "first_name")
    assert hasattr(prof, "email")
    assert hasattr(prof, "address_line1")
