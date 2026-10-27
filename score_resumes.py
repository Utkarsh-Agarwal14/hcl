
import json
import re
import shutil
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

RESUME_RESULTS_FILE = BASE_DIR / "results" / "resume_results.json"
JOB_DESCRIPTION_FILE = BASE_DIR / "job_description.json"

SCORES_FILE = BASE_DIR / "results" / "resume_scores.json"
EXCLUDED_FILE = BASE_DIR / "results" / "excluded_resumes.json"
TOP_RESUMES_DIR = BASE_DIR / "results" / "top_resumes"

DEFAULT_TOP_N = 5
DEFAULT_SKILLS_WEIGHT = 70
DEFAULT_EXPERIENCE_WEIGHT = 30


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(file_path):
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


def save_json(file_path, data):
    file_path.parent.mkdir(parents=True, exist_ok=True)

    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def normalize_text(value):
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip().lower()


def safe_number(value):
    if value is None or isinstance(value, bool):
        return None

    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# ============================================================
# READ THE EXISTING NESTED NVIDIA OUTPUT
# ============================================================

def get_candidate(resume):
    """
    NVIDIA output format:
    {
        "file_path": "...",
        "status": "success",
        "candidate": {
            "candidate_name": "...",
            "skills": [...],
            ...
        }
    }

    Return candidate details without modifying the source JSON.
    """

    candidate = resume.get("candidate", {})

    if isinstance(candidate, dict):
        return candidate

    return {}


def get_candidate_field(resume, field, default=None):
    return get_candidate(resume).get(field, default)


def get_resume_path(resume):
    # The original PDF path is outside the nested candidate object.
    path = resume.get("file_path")

    if not path:
        candidate = get_candidate(resume)
        path = candidate.get("file_path")

    if not path:
        return None

    return Path(path)


def get_resume_name(resume, index):
    name = get_candidate_field(resume, "candidate_name")

    if name and str(name).strip():
        return str(name).strip()

    path = get_resume_path(resume)

    if path:
        return path.stem

    return f"Candidate_{index}"


# ============================================================
# SKILL MATCHING
# ============================================================

def extract_candidate_skills(resume):
    skills = get_candidate_field(resume, "skills", [])

    if isinstance(skills, str):
        skills = [item.strip() for item in skills.split(",")]

    if not isinstance(skills, list):
        return set()

    return {
        normalize_text(skill)
        for skill in skills
        if normalize_text(skill)
    }


def skill_matches(required_skill, candidate_skills):
    

    required_skill = normalize_text(required_skill)

    if not required_skill:
        return False

    for candidate_skill in candidate_skills:
        if candidate_skill == required_skill:
            return True

        # Exact phrase or a phrase contained in a longer skill description.
        pattern = (
            r"(?<![a-z0-9+#.])"
            + re.escape(required_skill)
            + r"(?![a-z0-9+#.])"
        )

        if re.search(pattern, candidate_skill):
            return True

    return False


def score_skills(resume, job):
    candidate_skills = extract_candidate_skills(resume)

    required = [
        normalize_text(skill)
        for skill in job.get("required_skills", [])
        if normalize_text(skill)
    ]

    preferred = [
        normalize_text(skill)
        for skill in job.get("preferred_skills", [])
        if normalize_text(skill)
    ]

    # Required skills contribute 70% of the skills component.
    if required:
        matched_required = [
            skill for skill in required
            if skill_matches(skill, candidate_skills)
        ]

        required_score = (
            len(matched_required) / len(required)
        ) * 70
    else:
        matched_required = []
        required_score = 70

    # Preferred skills contribute the remaining 30%.
    if preferred:
        matched_preferred = [
            skill for skill in preferred
            if skill_matches(skill, candidate_skills)
        ]

        preferred_score = (
            len(matched_preferred) / len(preferred)
        ) * 30
    else:
        matched_preferred = []
        preferred_score = 30

    missing_required = [
        skill for skill in required
        if skill not in matched_required
    ]

    missing_preferred = [
        skill for skill in preferred
        if skill not in matched_preferred
    ]

    return {
        "score": round(required_score + preferred_score, 2),
        "matched_required_skills": matched_required,
        "missing_required_skills": missing_required,
        "matched_preferred_skills": matched_preferred,
        "missing_preferred_skills": missing_preferred,
        "candidate_skills": sorted(candidate_skills)
    }


# ============================================================
# EXPERIENCE EXTRACTION
# ============================================================

def parse_experience_estimate(text):
    """
    Parse examples such as:
    'Approximately 8 years and 0 months'
    '2 years'
    '6 years and 4 months'
    """

    text = normalize_text(text)

    match = re.search(
        r"(\d+)\s+years?(?:\s+and\s+(\d+)\s+months?)?",
        text
    )

    if not match:
        return None

    years = int(match.group(1))
    months = int(match.group(2) or 0)

    return years * 12 + months


def get_experience_months(resume):
    """
    Priority:
    1. Calculated experience from employment dates.
    2. Explicit estimated experience in months.
    3. Claimed experience in years.
    4. LLM's human-readable experience estimate.

    Estimated or claimed experience requires verification.
    """

    calculated = safe_number(
        get_candidate_field(
            resume, "calculated_experience_months"
        )
    )

    if calculated is not None and calculated >= 0:
        return {
            "months": calculated,
            "source": "calculated",
            "requires_review": not bool(
                get_candidate_field(
                    resume, "experience_dates_complete", False
                )
            )
        }

    estimated_months = safe_number(
        get_candidate_field(
            resume, "estimated_experience_months"
        )
    )

    if estimated_months is not None and estimated_months >= 0:
        return {
            "months": estimated_months,
            "source": "estimated",
            "requires_review": True
        }

    claimed_years = safe_number(
        get_candidate_field(
            resume, "claimed_experience_years"
        )
    )

    if claimed_years is not None and claimed_years >= 0:
        return {
            "months": claimed_years * 12,
            "source": "claimed",
            "requires_review": True
        }

    estimate_text = get_candidate_field(
        resume, "experience_estimate", ""
    )

    parsed_months = parse_experience_estimate(estimate_text)

    if parsed_months is not None:
        return {
            "months": parsed_months,
            "source": "llm_estimate",
            "requires_review": True
        }

    return {
        "months": None,
        "source": "unknown",
        "requires_review": True
    }


def score_experience(resume, minimum_years):
    experience = get_experience_months(resume)

    months = experience["months"]
    source = experience["source"]
    requires_review = experience["requires_review"]

    minimum_months = minimum_years * 12

    if months is None:
        return {
            "score": None,
            "eligible": False,
            "experience_months": None,
            "experience_years": None,
            "experience_source": source,
            "requires_review": True,
            "reason": "Experience information is unavailable."
        }

    meets_minimum = months >= minimum_months

    # Candidates meeting the threshold get 100 for this component.
    # Candidates below the threshold receive 0.
    experience_score = 100 if meets_minimum else 0

    if meets_minimum:
        reason = (
            "Meets the minimum experience requirement."
            if not requires_review
            else "Experience meets the threshold but needs verification."
        )
    else:
        reason = (
            f"Below minimum: approximately {months / 12:.2f} years; "
            f"required {minimum_years:.2f} years."
        )

    return {
        "score": experience_score,
        "eligible": meets_minimum,
        "experience_months": round(months, 2),
        "experience_years": round(months / 12, 2),
        "experience_source": source,
        "requires_review": requires_review,
        "reason": reason
    }


# ============================================================
# LOCATION CHECK
# ============================================================

def check_location(resume, job):
    policy = normalize_text(job.get("location_policy", "any"))
    job_location = normalize_text(job.get("location"))

    candidate_location = normalize_text(
        get_candidate_field(resume, "location")
        or get_candidate_field(resume, "current_location")
    )

    if policy not in ("required", "preferred", "any"):
        policy = "any"

    if policy == "any" or not job_location:
        return {
            "eligible": True,
            "requires_review": False,
            "candidate_location": candidate_location or None,
            "reason": "No location restriction."
        }

    if not candidate_location:
        return {
            "eligible": policy != "required",
            "requires_review": True,
            "candidate_location": None,
            "reason": "Candidate location is not available in the extracted data."
        }

    matches = (
        job_location in candidate_location
        or candidate_location in job_location
    )

    if policy == "required" and not matches:
        return {
            "eligible": False,
            "requires_review": False,
            "candidate_location": candidate_location,
            "reason": "Candidate does not match the required location."
        }

    return {
        "eligible": True,
        "requires_review": policy == "preferred" and not matches,
        "candidate_location": candidate_location,
        "reason": (
            "Location matches."
            if matches
            else "Preferred location does not match."
        )
    }


# ============================================================
# OVERALL SCORE
# ============================================================

def calculate_overall(
    skills_score,
    experience_result,
    skills_weight,
    experience_weight
):
    """
    If experience is unknown, use the skills score rather than
    treating unknown experience as a confirmed zero.
    """

    components = [(skills_score, skills_weight)]

    if experience_result["score"] is not None:
        components.append(
            (experience_result["score"], experience_weight)
        )

    total_weight = sum(weight for _, weight in components)

    if total_weight <= 0:
        return 0.0

    weighted_score = sum(
        score * weight for score, weight in components
    )

    return round(weighted_score / total_weight, 2)


# ============================================================
# SCORE ONE RESUME
# ============================================================

def score_candidate(
    resume,
    job,
    index,
    skills_weight,
    experience_weight
):
    candidate = get_candidate(resume)
    name = get_resume_name(resume, index)

    skills_result = score_skills(resume, job)

    minimum_years = safe_number(
        job.get("minimum_experience_years", 0)
    )

    if minimum_years is None or minimum_years < 0:
        minimum_years = 0

    experience_result = score_experience(
        resume,
        minimum_years
    )

    location_result = check_location(resume, job)

    overall_score = calculate_overall(
        skills_result["score"],
        experience_result,
        skills_weight,
        experience_weight
    )

    # Core eligibility is separate from whether manual verification is needed.
    eligible = (
        experience_result["eligible"]
        and location_result["eligible"]
    )

    original_flags = candidate.get("review_flags", [])

    if isinstance(original_flags, str):
        original_flags = [original_flags]

    review_reasons = list(original_flags) if isinstance(
        original_flags, list
    ) else []

    if experience_result["requires_review"]:
        review_reasons.append(
            "Experience data requires verification."
        )

    if location_result["requires_review"]:
        review_reasons.append(
            "Location data requires verification."
        )

    if eligible and not review_reasons:
        recommendation = "Eligible"
    elif eligible:
        recommendation = "Eligible - Verify Details"
    else:
        recommendation = "Review Required - Requirements Not Confirmed"

    # Keep the original LLM structure and metadata.
    result = dict(resume)

    # Add convenient top-level candidate fields to the SCORE OUTPUT only.
    # The original resume_results.json is never modified.
    result.update(candidate)

    result.update({
        "candidate_name": name,
        "file_path": resume.get(
            "file_path",
            candidate.get("file_path", "")
        ),
        "skills_score": skills_result["score"],
        "experience_score": experience_result["score"],
        "overall_score": overall_score,
        "matched_required_skills":
            skills_result["matched_required_skills"],
        "missing_required_skills":
            skills_result["missing_required_skills"],
        "matched_preferred_skills":
            skills_result["matched_preferred_skills"],
        "missing_preferred_skills":
            skills_result["missing_preferred_skills"],
        "experience_check": experience_result,
        "location_check": location_result,
        "eligible": eligible,
        "requires_review": bool(review_reasons) or not eligible,
        "review_reasons": sorted(set(review_reasons)),
        "recommendation": recommendation
    })

    return result


# ============================================================
# COPY SHORTLISTED PDFS
# ============================================================

def copy_top_resumes(selected):
    TOP_RESUMES_DIR.mkdir(parents=True, exist_ok=True)

    # Remove previous output so stale PDFs do not remain.
    for item in TOP_RESUMES_DIR.iterdir():
        if item.is_file() or item.is_symlink():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)

    copied = 0

    for rank, candidate in enumerate(selected, start=1):
        source = get_resume_path(candidate)

        if source is None or not source.exists():
            print(
                "WARNING: Resume PDF not found for "
                f"{candidate.get('candidate_name')}: {source}"
            )
            continue

        filename = source.name

        if candidate.get("requires_review") or not candidate.get("eligible"):
            filename = f"{source.stem}_REVIEW{source.suffix}"

        destination = TOP_RESUMES_DIR / f"{rank:02d}_{filename}"

        try:
            shutil.copy2(source, destination)
            candidate["shortlist_rank"] = rank
            candidate["copied_resume_path"] = str(destination)
            copied += 1
        except OSError as error:
            print(f"Could not copy {source}: {error}")

    return copied


# ============================================================
# MAIN
# ============================================================

def main():
    resumes = load_json(RESUME_RESULTS_FILE)
    job = load_json(JOB_DESCRIPTION_FILE)

    # Support either a list or a wrapper object.
    if isinstance(resumes, dict):
        resumes = resumes.get(
            "results",
            resumes.get("resumes", [])
        )

    if not isinstance(resumes, list):
        raise ValueError(
            "resume_results.json must contain a list of records."
        )

    weights = job.get("weights", {})

    skills_weight = safe_number(
        weights.get("skills", DEFAULT_SKILLS_WEIGHT)
    )

    experience_weight = safe_number(
        weights.get("experience", DEFAULT_EXPERIENCE_WEIGHT)
    )

    if skills_weight is None or skills_weight < 0:
        skills_weight = DEFAULT_SKILLS_WEIGHT

    if experience_weight is None or experience_weight < 0:
        experience_weight = DEFAULT_EXPERIENCE_WEIGHT

    weight_total = skills_weight + experience_weight

    if weight_total <= 0:
        skills_weight = DEFAULT_SKILLS_WEIGHT
        experience_weight = DEFAULT_EXPERIENCE_WEIGHT
    else:
        # Normalize weights so they sum to 100.
        skills_weight = skills_weight / weight_total * 100
        experience_weight = experience_weight / weight_total * 100

    top_n_value = safe_number(job.get("top_n", DEFAULT_TOP_N))
    top_n = max(1, int(top_n_value or DEFAULT_TOP_N))

    scored = []
    skipped = 0

    for index, resume in enumerate(resumes, start=1):
        if not isinstance(resume, dict):
            skipped += 1
            continue

        # Match the actual NVIDIA extraction format.
        if resume.get("status") != "success":
            skipped += 1
            continue

        if not isinstance(resume.get("candidate"), dict):
            skipped += 1
            print(
                "WARNING: Missing candidate object for "
                f"{resume.get('file_path', 'unknown file')}"
            )
            continue

        scored.append(
            score_candidate(
                resume,
                job,
                index,
                skills_weight,
                experience_weight
            )
        )

    # Ranking: higher score first; eligible candidates win ties.
    scored.sort(
        key=lambda item: (
            item["overall_score"],
            item["eligible"],
            not item["requires_review"]
        ),
        reverse=True
    )

    # First priority: eligible candidates without review flags.
    confirmed = [
        item for item in scored
        if item["eligible"] and not item["requires_review"]
    ]

    # Second priority: eligible candidates needing verification.
    eligible_review = [
        item for item in scored
        if item["eligible"] and item["requires_review"]
    ]

    # Third priority: candidates who do not meet or cannot confirm requirements.
    not_confirmed = [
        item for item in scored
        if not item["eligible"]
    ]

    ordered_candidates = confirmed + eligible_review + not_confirmed
    selected = ordered_candidates[:top_n]

    # Every scored resume not selected is saved to the excluded file.
    selected_ids = {id(item) for item in selected}
    excluded = [
        item for item in scored
        if id(item) not in selected_ids
    ]

    copied = copy_top_resumes(selected)

    save_json(SCORES_FILE, scored)
    save_json(EXCLUDED_FILE, excluded)

    print("\n========== RESUME SCORING COMPLETE ==========")
    print(f"Job title: {job.get('job_title', 'Not specified')}")
    print(f"Successfully scored: {len(scored)}")
    print(f"Skipped/failed records: {skipped}")
    print(f"Eligible without review: {len(confirmed)}")
    print(f"Eligible but needing review: {len(eligible_review)}")
    print(f"Not confirmed eligible: {len(not_confirmed)}")
    print(f"Requested shortlist size: {top_n}")
    print(f"Shortlisted records: {len(selected)}")
    print(f"PDFs copied successfully: {copied}")

    print(f"\nAll scores: {SCORES_FILE}")
    print(f"Not selected: {EXCLUDED_FILE}")
    print(f"Shortlisted PDFs: {TOP_RESUMES_DIR}")

    print("\nTOP CANDIDATES")

    for rank, candidate in enumerate(selected, start=1):
        if candidate["eligible"]:
            status = (
                "ELIGIBLE - VERIFY"
                if candidate["requires_review"]
                else "ELIGIBLE"
            )
        else:
            status = "REVIEW REQUIRED - NOT CONFIRMED ELIGIBLE"

        print(
            f"{rank}. {candidate['candidate_name']} | "
            f"Score: {candidate['overall_score']:.2f} | "
            f"Skills: {candidate['skills_score']:.2f} | "
            f"Experience: {candidate['experience_score']} | "
            f"{status}"
        )

    if len(selected) < top_n:
        print(
            f"\nOnly {len(selected)} usable resumes were found. "
            "Check resume_results.json and resume_errors.json."
        )


if __name__ == "__main__":
    main()
