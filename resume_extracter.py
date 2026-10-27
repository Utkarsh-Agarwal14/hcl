
# import os
# import re
# import json
# import base64
# import time
# from pathlib import Path
# from datetime import datetime

# import pymupdf
# from openai import OpenAI, APIStatusError, APIConnectionError

# # --------------------------------------------------
# # CONFIGURATION: NVIDIA hosted inference via OpenRouter
# # --------------------------------------------------

# MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

# BASE_DIR = Path(__file__).resolve().parent

# DATASET_DIRS = [
#     BASE_DIR / "datasets" / "Dhanashri-D",
#     BASE_DIR / "datasets" / "LithiVR",
# ]

# RESULTS_DIR = BASE_DIR / "results"
# RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# OUTPUT_FILE = RESULTS_DIR / "resume_results.json"
# ERROR_FILE = RESULTS_DIR / "resume_errors.json"

# api_key = os.environ.get("OPENROUTER_API_KEY")
# if not api_key:
#     raise RuntimeError(
#         "OPENROUTER_API_KEY is missing. Set it in PowerShell."
#     )

# client = OpenAI(
#     base_url="https://openrouter.ai/api/v1",
#     api_key=api_key,
#     timeout=180.0,
#     max_retries=0,
# )

# # --------------------------------------------------
# # PROMPT: image input, concise JSON output
# # --------------------------------------------------

# PROMPT = """
# You are a resume information extraction system.

# Read ALL supplied resume page images. Preserve relationships
# between columns and do not invent missing information.

# Return valid JSON only, with exactly these fields:

# {
#   "candidate_name": null,
#   "email": null,
#   "phone": null,
#   "skills": [],
#   "education_summary": null,
#   "professional_experience": [
#     {
#       "company": null,
#       "position": null,
#       "start_date": null,
#       "end_date": null
#     }
#   ],
#   "claimed_experience_years": null,
#   "projects_count": 0,
#   "review_flags": []
# }

# Rules:
# - professional_experience includes employment entries only.
# - Do not count projects, coursework, or education as employment.
# - Dates should use YYYY-MM when the month is known, or YYYY
#   when only the year is known.
# - Use "present" for a current position when explicitly indicated.
# - claimed_experience_years is the candidate's explicitly stated
#   professional experience duration, if available; otherwise null.
# - Count distinct projects, not individual responsibilities.
# - Flag missing employment dates, impossible date sequences,
#   placeholder information, or claims that clearly conflict with
#   the resume's own evidence.
# - Do not accuse candidates of dishonesty. Flags require review.
# - Use null for unknown scalar values and [] for empty lists.
# - Do not include the full resume text in the response.
# """


# # --------------------------------------------------
# # READ AND RENDER PDF PAGES AS IMAGES
# # --------------------------------------------------

# def render_pdf_pages(pdf_path):
#     """Render every PDF page to a base64 PNG image."""
#     images = []

#     with pymupdf.open(pdf_path) as pdf:
#         for page in pdf:
#             pix = page.get_pixmap(
#                 matrix=pymupdf.Matrix(1.5, 1.5),
#                 alpha=False,
#             )
#             image_bytes = pix.tobytes("png")
#             image_b64 = base64.b64encode(
#                 image_bytes
#             ).decode("utf-8")

#             images.append(
#                 "data:image/png;base64," + image_b64
#             )

#     return images


# # --------------------------------------------------
# # CALL NVIDIA MODEL USING IMAGES, NOT EXTRACTED TEXT
# # --------------------------------------------------

# def extract_resume(pdf_path):
#     image_urls = render_pdf_pages(pdf_path)

#     if not image_urls:
#         raise ValueError("PDF has no pages.")

#     content = [
#         {"type": "text", "text": PROMPT}
#     ]

#     for image_url in image_urls:
#         content.append({
#             "type": "image_url",
#             "image_url": {"url": image_url},
#         })

#     last_error = None

#     for attempt in range(3):
#         try:
#             response = client.chat.completions.create(
#                 model=MODEL,
#                 messages=[
#                     {
#                         "role": "user",
#                         "content": content,
#                     }
#                 ],
#                 max_tokens=1800,
#                 temperature=0,
#             )

#             if not response.choices:
#                 raise RuntimeError(
#                     "Provider returned no choices."
#                 )

#             output = response.choices[0].message.content

#             if not output or not output.strip():
#                 raise RuntimeError(
#                     "Model returned empty output."
#                 )

#             output = output.strip()

#             # Remove Markdown fences if present.
#             if output.startswith("```"):
#                 output = output.split("\n", 1)[1]
#                 output = output.rsplit("```", 1)[0].strip()

#             # Parse JSON only.
#             data = json.loads(output)

#             if not isinstance(data, dict):
#                 raise ValueError("Model output is not a JSON object.")

#             return data, response.model, len(image_urls)

#         except APIStatusError as exc:
#             last_error = exc

#             # Retry temporary provider/server failures.
#             if exc.status_code not in (408, 429, 500, 502, 503, 504):
#                 raise

#         except (APIConnectionError, json.JSONDecodeError, RuntimeError) as exc:
#             last_error = exc

#         if attempt < 2:
#             time.sleep(2 ** (attempt + 1))

#     raise RuntimeError(
#         f"Failed after 3 attempts: {last_error}"
#     )


# # --------------------------------------------------
# # EXPERIENCE CALCULATION
# # --------------------------------------------------

# def parse_month(value):
#     """
#     Return a month index for YYYY-MM or current-date markers.
#     Return None for year-only or unrecognized dates.
#     """
#     if not value:
#         return None

#     value = str(value).strip().lower()

#     if value in {"present", "current", "now", "ongoing"}:
#         today = datetime.now()
#         return today.year * 12 + today.month - 1

#     match = re.fullmatch(
#         r"(19\d{2}|20\d{2}|21\d{2})[-/](0?[1-9]|1[0-2])",
#         value,
#     )

#     if match:
#         year = int(match.group(1))
#         month = int(match.group(2))
#         return year * 12 + month - 1

#     return None


# def calculate_experience(jobs):
#     """
#     Calculate the union of employment intervals to avoid
#     double-counting overlapping jobs.

#     Year-only dates are insufficient for exact month calculation.
#     """
#     intervals = []
#     dates_complete = True
#     has_employment = bool(jobs)
#     flags = []

#     for job in jobs:
#         if not isinstance(job, dict):
#             dates_complete = False
#             flags.append("Employment entry needs review")
#             continue

#         start_raw = job.get("start_date")
#         end_raw = job.get("end_date")

#         start = parse_month(start_raw)
#         end = parse_month(end_raw)

#         if not start_raw or not end_raw:
#             dates_complete = False
#             flags.append("Employment dates missing")
#             continue

#         # Year-only dates cannot give exact duration.
#         if start is None or end is None:
#             dates_complete = False
#             flags.append(
#                 "Employment month missing or date format unclear"
#             )
#             continue

#         if end < start:
#             dates_complete = False
#             flags.append(
#                 "Employment end date precedes start date"
#             )
#             continue

#         intervals.append((start, end))

#     # If any employment period has incomplete dates, the total
#     # would be incomplete, so do not present it as exact.
#     if not has_employment:
#         return None, dates_complete, flags

#     if not dates_complete or not intervals:
#         return None, False, flags

#     intervals.sort()
#     merged = []

#     for start, end in intervals:
#         if not merged or start > merged[-1][1]:
#             merged.append([start, end])
#         else:
#             merged[-1][1] = max(merged[-1][1], end)

#     total_months = sum(
#         end - start for start, end in merged
#     )

#     return total_months, True, flags


# # --------------------------------------------------
# # NORMALIZE TO THE COMPACT OUTPUT SCHEMA
# # --------------------------------------------------

# def normalize_result(data):
#     jobs = data.get("professional_experience")
#     if not isinstance(jobs, list):
#         jobs = []

#     months, dates_complete, date_flags = calculate_experience(jobs)

#     review_flags = data.get("review_flags")
#     if not isinstance(review_flags, list):
#         review_flags = []

#     # Remove duplicate flags while preserving order.
#     review_flags = list(dict.fromkeys(
#         str(flag) for flag in review_flags + date_flags
#     ))

#     claimed_years = data.get("claimed_experience_years")

#     # A simple estimate, not an exact calculation.
#     if months is not None:
#         experience_estimate = (
#             f"{months // 12} years and {months % 12} months"
#         )
#     elif jobs:
#         experience_estimate = (
#             "Exact duration unavailable; employment dates "
#             "need month-level detail"
#         )
#     else:
#         experience_estimate = (
#             "No employment experience could be calculated"
#         )

#     skills = data.get("skills")
#     if not isinstance(skills, list):
#         skills = []

#     education = data.get("education_summary")

#     return {
#         "candidate_name": data.get("candidate_name"),
#         "email": data.get("email"),
#         "phone": data.get("phone"),
#         "skills": skills,
#         "education_summary": education,
#         "employment_count": len(jobs),
#         "professional_experience": jobs,
#         "claimed_experience_years": claimed_years,
#         "calculated_experience_months": months,
#         "experience_estimate": experience_estimate,
#         "experience_dates_complete": dates_complete,
#         "review_flags": review_flags,
#         "projects_count": data.get("projects_count", 0),
#         "independently_verified": False,
#     }


# # --------------------------------------------------
# # PROCESS ALL PDF RESUMES
# # --------------------------------------------------

# def save_json(path, data):
#     path.write_text(
#         json.dumps(data, indent=2, ensure_ascii=False),
#         encoding="utf-8",
#     )


# def main():
#     resume_files = []

#     for folder in DATASET_DIRS:
#         if not folder.exists():
#             print(f"Dataset folder not found: {folder}")
#             continue

#         resume_files.extend(
#             path for path in folder.rglob("*.pdf")
#             if path.is_file()
#         )

#     resume_files = sorted(resume_files)

#     if not resume_files:
#         print("No PDF resumes found in the dataset folders.")
#         return

#     results = []
#     errors = []

#     # Resume progress if the script is rerun after interruption.
#     if OUTPUT_FILE.exists():
#         try:
#             previous = json.loads(
#                 OUTPUT_FILE.read_text(encoding="utf-8")
#             )
#             if isinstance(previous, list):
#                 results = previous
#         except (json.JSONDecodeError, OSError):
#             results = []

#     completed = {
#         item.get("file_path")
#         for item in results
#         if item.get("status") == "success"
#     }

#     print(f"Found {len(resume_files)} PDF resumes.")
#     print(f"Already completed: {len(completed)}")

#     for index, path in enumerate(resume_files, start=1):
#         relative_path = str(path.relative_to(BASE_DIR))

#         if relative_path in completed:
#             print(f"[{index}/{len(resume_files)}] Skipping {path.name}")
#             continue

#         print(f"[{index}/{len(resume_files)}] {path.name}")

#         try:
#             extracted, actual_model, page_count = extract_resume(path)
#             candidate = normalize_result(extracted)

#             record = {
#                 "file_path": relative_path,
#                 "status": "success",
#                 "model": actual_model,
#                 "pages_processed": page_count,
#                 "candidate": candidate,
#             }

#             results.append(record)
#             completed.add(relative_path)

#             print(
#                 f"  Success: {candidate['candidate_name']} "
#                 f"| Experience months: "
#                 f"{candidate['calculated_experience_months']}"
#             )

#         except Exception as exc:
#             errors.append({
#                 "file_path": relative_path,
#                 "error": str(exc),
#             })
#             print(f"  Failed: {exc}")

#         # Save after every resume so successes are preserved.
#         save_json(OUTPUT_FILE, results)
#         save_json(ERROR_FILE, errors)

#         time.sleep(1)

#     print("\nProcessing finished.")
#     print(f"Successful records: {len(results)}")
#     print(f"Errors this run: {len(errors)}")
#     print(f"Results saved to: {OUTPUT_FILE}")
#     print(f"Errors saved to: {ERROR_FILE}")


# if __name__ == "__main__":
#     main()


import os
import re
import json
import base64
import time
from pathlib import Path
from datetime import datetime

import pymupdf
from openai import OpenAI, APIStatusError, APIConnectionError


# =========================================================
# CONFIGURATION
# =========================================================

MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

BASE_DIR = Path(__file__).resolve().parent

DATASET_DIRS = [
    BASE_DIR / "datasets" / "Dhanashri-D",
    BASE_DIR / "datasets" / "LithiVR",
]

RESULTS_DIR = BASE_DIR / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FILE = RESULTS_DIR / "resume_results.json"
ERROR_FILE = RESULTS_DIR / "resume_errors.json"

api_key = os.environ.get("OPENROUTER_API_KEY")

if not api_key:
    raise RuntimeError(
        "OPENROUTER_API_KEY is missing. Set it in PowerShell."
    )

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=api_key,
    timeout=180.0,
    max_retries=0,
)


# =========================================================
# NVIDIA EXTRACTION PROMPT
# =========================================================

PROMPT = """
You are a resume information extraction system.

Read ALL supplied resume page images. Preserve relationships
between columns. Do not invent information.

Return valid JSON only, with exactly these fields:

{
  "candidate_name": null,
  "email": null,
  "phone": null,
  "skills": [],
  "education_summary": null,
  "professional_experience": [
    {
      "company": null,
      "position": null,
      "start_date": null,
      "end_date": null
    }
  ],
  "claimed_experience_years": null,
  "projects_count": 0,
  "review_flags": []
}

IMPORTANT DATE RULES:

1. Never omit a job just because its dates do not include months.
2. Preserve year-only employment dates:
   "2026-2027" -> start_date: "2026", end_date: "2027"
   "2026-27"   -> start_date: "2026", end_date: "2027"
   "2023-Present" -> start_date: "2023", end_date: "present"
3. Use YYYY-MM if the month is shown, otherwise YYYY.
4. Normalize two-digit end years using the start century where
   appropriate. Example: 2026-27 means 2026 to 2027.
5. Do not invent months.
6. Extract all employment entries, including current jobs.
7. Keep projects separate from professional employment.
8. claimed_experience_years means the candidate's explicitly
   stated total professional experience, if available.
   Otherwise use null.
9. Count distinct projects, not responsibilities.
10. Add cautious review flags for unclear dates, impossible
    date sequences, placeholder information, or explicit
    inconsistencies. Do not accuse a candidate of dishonesty.
11. Use null for unavailable scalar values and [] for empty lists.
12. Do not return full resume text or long job descriptions.
"""


# =========================================================
# RENDER PDF PAGES AS IMAGES
# =========================================================

def render_pdf_pages(pdf_path):
    """Render every PDF page to a base64 PNG image."""

    images = []

    with pymupdf.open(pdf_path) as pdf:
        for page in pdf:
            pix = page.get_pixmap(
                matrix=pymupdf.Matrix(1.5, 1.5),
                alpha=False,
            )

            image_bytes = pix.tobytes("png")
            image_b64 = base64.b64encode(
                image_bytes
            ).decode("utf-8")

            images.append(
                "data:image/png;base64," + image_b64
            )

    return images


# =========================================================
# CALL NVIDIA THROUGH OPENROUTER
# =========================================================

def extract_resume(pdf_path):
    image_urls = render_pdf_pages(pdf_path)

    if not image_urls:
        raise ValueError("PDF contains no pages.")

    content = [{"type": "text", "text": PROMPT}]

    for image_url in image_urls:
        content.append({
            "type": "image_url",
            "image_url": {"url": image_url},
        })

    last_error = None

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": content,
                    }
                ],
                max_tokens=1800,
                temperature=0,
            )

            if not response.choices:
                raise RuntimeError("Provider returned no choices.")

            output = response.choices[0].message.content

            if not output or not output.strip():
                raise RuntimeError("Model returned empty output.")

            output = output.strip()

            # Remove Markdown fences if the model adds them.
            if output.startswith("```"):
                output = output.split("\n", 1)[1]
                output = output.rsplit("```", 1)[0].strip()

            data = json.loads(output)

            if not isinstance(data, dict):
                raise ValueError("Model output must be a JSON object.")

            # Record the actual model used by the provider.
            return data, response.model, len(image_urls)

        except APIStatusError as exc:
            last_error = exc

            # Retry temporary provider/server failures only.
            if exc.status_code not in (
                408, 429, 500, 502, 503, 504
            ):
                raise

        except (APIConnectionError, json.JSONDecodeError,
                RuntimeError) as exc:
            last_error = exc

        if attempt < 2:
            wait_seconds = 2 ** (attempt + 1)
            print(f"  Retry in {wait_seconds}s: {last_error}")
            time.sleep(wait_seconds)

    raise RuntimeError(f"Failed after 3 attempts: {last_error}")


# =========================================================
# DATE PARSING
# =========================================================

def normalize_date(value, is_end=False, start_year=None):
    """
    Normalize date values.

    Examples:
        2026-2027 -> handled as start 2026, end 2027
        2026-27   -> handled as start 2026, end 2027
        2025-06   -> 2025-06
        Present   -> present

    Return (normalized_value, month_index, has_month).
    """

    if value is None:
        return None, None, False

    value = str(value).strip().lower()

    if not value:
        return None, None, False

    if value in {"present", "current", "now", "ongoing"}:
        today = datetime.now()
        month_index = today.year * 12 + today.month - 1
        return "present", month_index, True

    # Normalize en dash and em dash.
    value = value.replace("–", "-").replace("—", "-")

    # Full year and month: YYYY-MM or YYYY/MM.
    match = re.fullmatch(
        r"(19\d{2}|20\d{2}|21\d{2})[-/](0?[1-9]|1[0-2])",
        value,
    )

    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        normalized = f"{year:04d}-{month:02d}"
        return normalized, year * 12 + month - 1, True

    # Four-digit year.
    match = re.fullmatch(r"(19\d{2}|20\d{2}|21\d{2})", value)

    if match:
        year = int(match.group(1))

        # Year-only dates use January for a start and December
        # for an end solely to form a broad duration estimate.
        month = 12 if is_end else 1
        month_index = year * 12 + month - 1

        return str(year), month_index, False

    # Two-digit year, e.g. 27.
    match = re.fullmatch(r"\d{2}", value)

    if match and start_year is not None:
        two_digit_year = int(value)
        century = (start_year // 100) * 100
        year = century + two_digit_year

        # If the resulting end year is earlier than the start,
        # move to the next century only for a genuine rollover.
        if year < start_year:
            year += 100

        month = 12 if is_end else 1
        month_index = year * 12 + month - 1

        return str(year), month_index, False

    return value, None, False


def split_date_range(start_value, end_value):
    """
    Handle ranges accidentally returned in one date field,
    such as '2026-2027' or '2026-27'.
    """

    if not start_value:
        return start_value, end_value

    value = str(start_value).strip()
    value = value.replace("–", "-").replace("—", "-")

    # A range such as 2026-2027 or 2026-27.
    match = re.fullmatch(
        r"(19\d{2}|20\d{2}|21\d{2})\s*-\s*(\d{2}|\d{4})",
        value,
    )

    if match and not end_value:
        start_year = int(match.group(1))
        end_part = match.group(2)

        if len(end_part) == 2:
            end_year = (start_year // 100) * 100 + int(end_part)

            if end_year < start_year:
                end_year += 100
        else:
            end_year = int(end_part)

        return str(start_year), str(end_year)

    return start_value, end_value


# =========================================================
# CALCULATE PROFESSIONAL EXPERIENCE
# =========================================================

def calculate_experience(jobs):
    """
    Merge overlapping employment intervals.

    Exact months are returned only if every included employment
    period has month-level dates.

    Year-only dates contribute to an approximate estimate,
    but never to the exact calculated_experience_months field.

    Returns:
        exact_months, estimated_months, dates_complete, flags
    """

    intervals = []
    all_months_known = True
    flags = []

    for job in jobs:
        if not isinstance(job, dict):
            all_months_known = False
            flags.append("Employment entry needs review")
            continue

        start_raw = job.get("start_date")
        end_raw = job.get("end_date")

        start_raw, end_raw = split_date_range(
            start_raw, end_raw
        )

        # Preserve normalized values in the saved employment record.
        start_normalized, start_index, start_has_month = (
            normalize_date(start_raw)
        )

        # Parse two-digit end years using the start year as context.
        start_year = None

        if start_normalized and re.fullmatch(
            r"\d{4}", start_normalized
        ):
            start_year = int(start_normalized)
        elif start_normalized and re.fullmatch(
            r"\d{4}-\d{2}", start_normalized
        ):
            start_year = int(start_normalized[:4])

        end_normalized, end_index, end_has_month = normalize_date(
            end_raw,
            is_end=True,
            start_year=start_year,
        )

        job["start_date"] = start_normalized
        job["end_date"] = end_normalized

        if start_index is None or end_index is None:
            all_months_known = False
            flags.append(
                "Employment dates missing or unclear"
            )
            continue

        if end_index < start_index:
            all_months_known = False
            flags.append(
                "Employment end date precedes start date"
            )
            continue

        if not start_has_month or not end_has_month:
            all_months_known = False
            flags.append(
                "Employment duration estimated from year-only dates"
            )

        intervals.append((start_index, end_index))

        # Future-dated work is flagged for review.
        today = datetime.now()
        current_month_index = today.year * 12 + today.month - 1

        if start_index > current_month_index:
            flags.append("Employment start date is in the future")
        elif end_index > current_month_index:
            flags.append(
                "Employment end date is in the future; verify status"
            )

    if not intervals:
        return None, None, False, flags

    intervals.sort()
    merged = []

    for start, end in intervals:
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    # Inclusive month counting for approximate screening.
    estimated_months = sum(
        end - start + 1 for start, end in merged
    )

    if all_months_known:
        return estimated_months, estimated_months, True, flags

    return None, estimated_months, False, flags


# =========================================================
# COMPACT JSON OUTPUT
# =========================================================

def normalize_result(data):
    jobs = data.get("professional_experience")

    if not isinstance(jobs, list):
        jobs = []

    months, estimated_months, dates_complete, date_flags = (
        calculate_experience(jobs)
    )

    review_flags = data.get("review_flags")

    if not isinstance(review_flags, list):
        review_flags = []

    review_flags = list(dict.fromkeys(
        [str(flag) for flag in review_flags + date_flags]
    ))

    if months is not None:
        experience_estimate = (
            f"{months // 12} years and {months % 12} months"
        )
    elif estimated_months is not None:
        experience_estimate = (
            f"Approximately {estimated_months // 12} years "
            f"and {estimated_months % 12} months "
            "(estimated from incomplete dates)"
        )
    else:
        experience_estimate = (
            "Unable to estimate from available employment dates"
        )

    skills = data.get("skills")
    if not isinstance(skills, list):
        skills = []

    education = data.get("education_summary")

    projects_count = data.get("projects_count", 0)

    try:
        projects_count = max(0, int(projects_count))
    except (ValueError, TypeError):
        projects_count = 0

    return {
        "candidate_name": data.get("candidate_name"),
        "email": data.get("email"),
        "phone": data.get("phone"),
        "skills": skills,
        "education_summary": education,
        "employment_count": len(jobs),
        "professional_experience": jobs,
        "claimed_experience_years": data.get(
            "claimed_experience_years"
        ),
        "calculated_experience_months": months,
        "experience_estimate": experience_estimate,
        "experience_dates_complete": dates_complete,
        "review_flags": review_flags,
        "projects_count": projects_count,
        "independently_verified": False,
    }


# =========================================================
# JSON FILE HELPERS
# =========================================================

def save_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_json(path, default):
    if not path.exists():
        return default

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return default


# =========================================================
# PROCESS ALL DATASET PDFS
# =========================================================

def main():
    resume_files = []

    for folder in DATASET_DIRS:
        if not folder.exists():
            print(f"Dataset folder not found: {folder}")
            continue

        resume_files.extend(
            path for path in folder.rglob("*")
            if path.is_file()
            and path.suffix.lower() == ".pdf"
        )

    resume_files = sorted(resume_files)

    if not resume_files:
        print("No PDF resumes found in the dataset folders.")
        return

    results = load_json(OUTPUT_FILE, [])
    errors = load_json(ERROR_FILE, [])

    if not isinstance(results, list):
        results = []

    if not isinstance(errors, list):
        errors = []

    # Skip files that have already succeeded.
    completed = {
        item.get("file_path")
        for item in results
        if item.get("status") == "success"
    }

    # Keep only the latest error per file when rerunning.
    errors_by_path = {
        item.get("file_path"): item
        for item in errors
        if isinstance(item, dict) and item.get("file_path")
    }

    print(f"Found {len(resume_files)} PDF resumes.")
    print(f"Already completed: {len(completed)}")

    for index, path in enumerate(resume_files, start=1):
        relative_path = str(path.relative_to(BASE_DIR))

        if relative_path in completed:
            print(
                f"[{index}/{len(resume_files)}] "
                f"Skipping {path.name}"
            )
            continue

        print(f"\n[{index}/{len(resume_files)}] {path.name}")

        try:
            extracted, actual_model, page_count = extract_resume(path)
            candidate = normalize_result(extracted)

            record = {
                "file_path": relative_path,
                "status": "success",
                "model": actual_model,
                "pages_processed": page_count,
                "candidate": candidate,
            }

            results.append(record)
            completed.add(relative_path)
            errors_by_path.pop(relative_path, None)

            print(f"  Model: {actual_model}")
            print(f"  Candidate: {candidate['candidate_name']}")
            print(f"  Employment entries: {candidate['employment_count']}")
            print(
                "  Exact experience months: "
                f"{candidate['calculated_experience_months']}"
            )
            print(
                f"  Estimate: {candidate['experience_estimate']}"
            )

        except Exception as exc:
            print(f"  Failed: {exc}")

            errors_by_path[relative_path] = {
                "file_path": relative_path,
                "error": str(exc),
            }

        # Preserve all successes and errors after each resume.
        save_json(OUTPUT_FILE, results)
        save_json(ERROR_FILE, list(errors_by_path.values()))

        time.sleep(1)

    print("\nProcessing finished.")
    print(f"PDFs discovered: {len(resume_files)}")
    print(f"Successful records: {len(completed)}")
    print(f"Errors recorded: {len(errors_by_path)}")
    print(f"Results: {OUTPUT_FILE}")
    print(f"Errors: {ERROR_FILE}")


if __name__ == "__main__":
    main()
