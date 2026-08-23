import ollama
import os
import json
from datetime import datetime


MODEL = "qwen2.5:1.5b"
DATA_FOLDER = "data"
SCHEDULE_FILE = os.path.join(DATA_FOLDER, "schedule.json")
ANALYTICS_FILE = os.path.join(DATA_FOLDER, "analytics.json")


os.makedirs(DATA_FOLDER, exist_ok=True)


def show_menu():
    print("\n==============================")
    print("      SOCIAL MEDIA BOT")
    print("==============================")
    print("1 - Content Creator")
    print("2 - Caption Generator")
    print("3 - Comment Generator")
    print("4 - Scheduler")
    print("5 - Analytics")
    print("6 - Instagram")
    print("0 - Exit")
    print("==============================")


def ask_ai(system_prompt, user_prompt):
    try:
        print("\nAI is thinking...\n")

        response = ollama.chat(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": user_prompt
                }
            ]
        )

        return response["message"]["content"]

    except Exception as error:
        print("\nAI ERROR:")
        print(error)
        return None


def save_result(category, topic, result):
    save = input("\nSave result? (y/n): ").strip().lower()

    if save != "y":
        print("Result not saved.")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{category}_{timestamp}.txt"
    filepath = os.path.join(DATA_FOLDER, filename)

    with open(filepath, "w", encoding="utf-8") as file:
        file.write(f"TYPE: {category}\n")
        file.write(
            f"DATE: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        file.write(f"TOPIC: {topic}\n\n")
        file.write(result)

    print(f"\nSaved successfully: {filepath}")


def generate_content():
    topic = input("\nEnter your content topic: ").strip()

    if not topic:
        print("Topic cannot be empty.")
        return

    system_prompt = (
        "You are a professional Instagram content strategist. "
        "Create original, practical and engaging social media ideas. "
        "Avoid generic ideas."
    )

    user_prompt = (
        f"Create exactly 3 Instagram content ideas about: {topic}.\n\n"
        "For each idea return exactly this structure:\n"
        "Title:\n"
        "Concept:\n"
        "Format:\n"
        "Visual Direction:\n\n"
        "Do not add extra sections."
    )

    result = ask_ai(system_prompt, user_prompt)

    if result is None:
        return

    print("\n==============================")
    print("         AI RESULT")
    print("==============================\n")
    print(result)
    print("\n==============================")

    save_result("content", topic, result)


def generate_caption():
    topic = input("\nWhat is the post about? ").strip()

    if not topic:
        print("Topic cannot be empty.")
        return

    tone = input(
        "Choose tone (professional / cinematic / simple / engaging): "
    ).strip()

    if not tone:
        tone = "engaging"

    language = input(
        "Language (English / French / Arabic): "
    ).strip()

    if not language:
        language = "English"

    system_prompt = (
        "You are a professional Instagram copywriter. "
        "Follow the requested format exactly. "
        "Create only ONE caption."
    )

    user_prompt = (
        f"Post topic: {topic}\n"
        f"Tone: {tone}\n"
        f"Language: {language}\n\n"
        "Create exactly ONE Instagram caption.\n\n"
        "Return exactly:\n\n"
        "CAPTION:\n"
        "<caption>\n\n"
        "CTA:\n"
        "<short call to action>\n\n"
        "HASHTAGS:\n"
        "<exactly 5 hashtags>\n\n"
        "Do not create alternatives."
    )

    result = ask_ai(system_prompt, user_prompt)

    if result is None:
        return

    print("\n==============================")
    print("        CAPTION RESULT")
    print("==============================\n")
    print(result)
    print("\n==============================")

    save_result("caption", topic, result)


def generate_comments():
    post_description = input(
        "\nDescribe the Instagram post you want to comment on: "
    ).strip()

    if not post_description:
        print("Post description cannot be empty.")
        return

    style = input(
        "Comment style (natural / professional / friendly / short): "
    ).strip()

    if not style:
        style = "natural"

    language = input(
        "Language (English / French / Arabic): "
    ).strip()

    if not language:
        language = "English"

    system_prompt = (
        "You create natural Instagram comments. "
        "Comments must sound human and relevant. "
        "Never use hashtags. "
        "Avoid repetitive or spammy comments."
    )

    user_prompt = (
        f"Post description: {post_description}\n"
        f"Style: {style}\n"
        f"Language: {language}\n\n"
        "Create exactly 5 different Instagram comments.\n"
        "Each comment must be under 15 words.\n"
        "Do NOT use hashtags.\n"
        "Use maximum one emoji per comment.\n"
        "Make every comment different.\n\n"
        "Return only comments numbered 1 to 5."
    )

    result = ask_ai(system_prompt, user_prompt)

    if result is None:
        return

    print("\n==============================")
    print("        COMMENT RESULT")
    print("==============================\n")
    print(result)
    print("\n==============================")

    save_result("comments", post_description, result)


def get_saved_files():
    files = []

    for filename in os.listdir(DATA_FOLDER):
        if filename.endswith(".txt"):
            files.append(filename)

    files.sort()
    return files


def load_schedule():
    if not os.path.exists(SCHEDULE_FILE):
        return []

    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except:
        return []


def save_schedule(schedule):
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as file:
        json.dump(
            schedule,
            file,
            indent=4,
            ensure_ascii=False
        )


def show_scheduled_posts():
    schedule = load_schedule()

    if not schedule:
        print("\nNo scheduled posts.")
        return

    print("\n==============================")
    print("       SCHEDULED POSTS")
    print("==============================")

    for index, item in enumerate(schedule, start=1):
        print(f"\n{index}. File: {item['file']}")
        print(f"   Date: {item['date']}")
        print(f"   Time: {item['time']}")
        print(f"   Status: {item['status']}")

    print("\n==============================")


def scheduler():
    while True:
        print("\n==============================")
        print("          SCHEDULER")
        print("==============================")
        print("1 - Schedule a saved file")
        print("2 - View scheduled posts")
        print("0 - Back")
        print("==============================")

        choice = input("Choose: ").strip()

        if choice == "1":
            saved_files = get_saved_files()

            if not saved_files:
                print("\nNo saved content found.")
                continue

            print("\nSaved files:\n")

            for index, filename in enumerate(saved_files, start=1):
                print(f"{index} - {filename}")

            try:
                file_choice = int(
                    input("\nChoose file number: ").strip()
                )

                if file_choice < 1 or file_choice > len(saved_files):
                    print("Invalid file number.")
                    continue

            except ValueError:
                print("Please enter a number.")
                continue

            selected_file = saved_files[file_choice - 1]

            date_input = input(
                "Enter date (YYYY-MM-DD): "
            ).strip()

            time_input = input(
                "Enter time (HH:MM): "
            ).strip()

            try:
                datetime.strptime(
                    f"{date_input} {time_input}",
                    "%Y-%m-%d %H:%M"
                )

            except ValueError:
                print("\nInvalid date or time format.")
                continue

            schedule = load_schedule()

            schedule.append({
                "file": selected_file,
                "date": date_input,
                "time": time_input,
                "status": "pending"
            })

            save_schedule(schedule)

            print("\nScheduled successfully.")
            print(f"File: {selected_file}")
            print(f"Date: {date_input}")
            print(f"Time: {time_input}")

        elif choice == "2":
            show_scheduled_posts()

        elif choice == "0":
            break

        else:
            print("\nInvalid choice.")


def load_analytics():
    if not os.path.exists(ANALYTICS_FILE):
        return []

    try:
        with open(ANALYTICS_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except:
        return []


def save_analytics(data):
    with open(ANALYTICS_FILE, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            indent=4,
            ensure_ascii=False
        )


def add_post_analytics():
    print("\n==============================")
    print("      ADD POST ANALYTICS")
    print("==============================")

    name = input("Post name/title: ").strip()

    if not name:
        print("Post name cannot be empty.")
        return

    date_input = input("Post date (YYYY-MM-DD): ").strip()

    try:
        datetime.strptime(date_input, "%Y-%m-%d")
    except ValueError:
        print("Invalid date.")
        return

    try:
        likes = int(input("Likes: ").strip())
        comments = int(input("Comments: ").strip())
        views = int(input("Views / Reach: ").strip())

        if likes < 0 or comments < 0 or views < 0:
            print("Numbers cannot be negative.")
            return

    except ValueError:
        print("Please enter valid numbers.")
        return

    data = load_analytics()

    data.append({
        "name": name,
        "date": date_input,
        "likes": likes,
        "comments": comments,
        "views": views
    })

    save_analytics(data)

    print("\nAnalytics saved successfully.")


def show_analytics():
    data = load_analytics()

    if not data:
        print("\nNo analytics data yet.")
        return

    print("\n==============================")
    print("          ANALYTICS")
    print("==============================")

    total_likes = 0
    total_comments = 0
    total_views = 0

    best_post = None
    best_score = -1

    for index, post in enumerate(data, start=1):
        likes = post["likes"]
        comments = post["comments"]
        views = post["views"]

        total_likes += likes
        total_comments += comments
        total_views += views

        score = likes + (comments * 2)

        if score > best_score:
            best_score = score
            best_post = post

        print(f"\n{index}. {post['name']}")
        print(f"   Date: {post['date']}")
        print(f"   Likes: {likes}")
        print(f"   Comments: {comments}")
        print(f"   Views/Reach: {views}")

        if views > 0:
            engagement = ((likes + comments) / views) * 100
            print(f"   Engagement: {engagement:.2f}%")
        else:
            print("   Engagement: N/A")

    print("\n==============================")
    print("          SUMMARY")
    print("==============================")
    print(f"Posts tracked: {len(data)}")
    print(f"Total likes: {total_likes}")
    print(f"Total comments: {total_comments}")
    print(f"Total views/reach: {total_views}")

    if best_post:
        print(f"Best post: {best_post['name']}")

    print("==============================")


def analytics_menu():
    while True:
        print("\n==============================")
        print("          ANALYTICS")
        print("==============================")
        print("1 - Add post statistics")
        print("2 - View analytics")
        print("0 - Back")
        print("==============================")

        choice = input("Choose: ").strip()

        if choice == "1":
            add_post_analytics()

        elif choice == "2":
            show_analytics()

        elif choice == "0":
            break

        else:
            print("\nInvalid choice.")


def main():
    while True:
        show_menu()

        choice = input("Choose: ").strip()

        if choice == "1":
            generate_content()

        elif choice == "2":
            generate_caption()

        elif choice == "3":
            generate_comments()

        elif choice == "4":
            scheduler()

        elif choice == "5":
            analytics_menu()

        elif choice == "6":
            print("\nInstagram - Coming soon")

        elif choice == "0":
            print("\nBot stopped.")
            break

        else:
            print("\nInvalid choice.")


if __name__ == "__main__":
    main()