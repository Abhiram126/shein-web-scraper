"""
User Category Selection
"""

from category_config import MAIN_CATEGORIES


def select_categories():
    """
    Display all available categories and return the selected category URLs.
    """

    print("\n" + "=" * 45)
    print("      SHEIN PRODUCT SCRAPER")
    print("=" * 45)
    print("\nSelect Categories:\n")

    # Display categories
    for number, category in MAIN_CATEGORIES.items():
        print(f"{number}. {category['name']}")

    print("\nExample:")
    print("1")
    print("1,3")
    print("2,5,7")

    choice = input("\nEnter category numbers: ").strip()

    selected_urls = []

    try:
        numbers = [int(x.strip()) for x in choice.split(",")]

        for num in numbers:
            if num in MAIN_CATEGORIES:
                selected_urls.append(MAIN_CATEGORIES[num]["url"])
            else:
                print(f"Invalid category: {num}")

    except ValueError:
        print("Invalid input.")
        return []

    return selected_urls

if __name__ == "__main__":
    selected = select_categories()

    print("\nSelected URLs:")
    print(selected)