from app.services.product_presentation import clean_product_title, product_image_url


def test_clean_product_title_removes_public_sales_and_price_suffixes():
    assert clean_product_title("Desk Organizer 11 sold $ 1.09 New shoppers save $4") == "Desk Organizer"
    assert clean_product_title("Desk Organizer HK$ 52.32") == "Desk Organizer"
    assert clean_product_title("Desk Organizer ¥ 52.32") == "Desk Organizer"
    assert clean_product_title("Desk Organizer € 4.25") == "Desk Organizer"
    assert clean_product_title("Toothbrush Holder 已售 1,200") == "Toothbrush Holder"
    assert clean_product_title("Plain Product Title") == "Plain Product Title"


def test_product_image_url_reads_explicit_and_structured_public_images():
    assert product_image_url({"main_image_url": "https://cdn.example/main.jpg"}) == "https://cdn.example/main.jpg"
    data = {"structured_objects": [{"image": ["https://cdn.example/jsonld.jpg"]}]}
    assert product_image_url(data) == "https://cdn.example/jsonld.jpg"


def test_product_image_url_reads_nested_extension_and_alias_fields():
    data = {
        "extension_raw_data": {
            "products": [{"imageUrl": "https://cdn.example/discovered-main.jpg"}]
        }
    }
    assert product_image_url(data) == "https://cdn.example/discovered-main.jpg"


def test_product_image_url_ignores_non_product_icon_urls():
    data = {
        "image_url": "https://cdn.example/store-logo.png",
        "structured_objects": [{"image": "https://cdn.example/product-main.jpg"}],
    }
    assert product_image_url(data) == "https://cdn.example/product-main.jpg"


def test_product_image_url_does_not_treat_discovered_product_link_as_an_image():
    data = {
        "url": "https://www.aliexpress.com/item/1005001234567890.html",
        "extension_raw_data": {"products": [{"url": "https://www.aliexpress.com/item/1005001234567891.html"}]},
    }
    assert product_image_url(data) is None
