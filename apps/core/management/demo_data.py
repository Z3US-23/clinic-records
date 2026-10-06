"""Content library for the `seed_demo` command: names, places, medicines and typical consultations.

Everything here is plain data plus a few small helpers. Nothing touches the database, so this file
can be read (or extended) on its own. All people, numbers and records are made up.
"""

from dataclasses import dataclass
from decimal import Decimal

# --- People and places -------------------------------------------------------------------------

# First names by generation, so grandparents and children get names typical of their age.
MALE_NAMES = {
    "child": ["Ayaan", "Zain", "Rayyan", "Abdul Hadi", "Muhammad Ahmed", "Hamza", "Ibrahim", "Saad", "Arham", "Zaid"],
    "adult": [
        "Bilal", "Usman", "Hassan", "Imran", "Kashif", "Faisal", "Naveed", "Zubair", "Waqas", "Adnan", "Junaid",
        "Rizwan", "Sohail", "Fahad", "Talha", "Umar", "Yasir", "Shahzad", "Kamran", "Asad", "Danish", "Salman",
    ],
    "elder": [
        "Ghulam Rasool", "Muhammad Aslam", "Allah Ditta", "Manzoor Hussain", "Abdul Ghafoor", "Bashir Ahmed",
        "Muhammad Sharif", "Nazir Ahmed", "Khadim Hussain", "Abdul Rasheed",
    ],
}
FEMALE_NAMES = {
    "child": ["Hoorain", "Anaya", "Areeba", "Zoya", "Eshaal", "Aiza", "Fatima", "Inaya", "Haniya", "Maham"],
    "adult": [
        "Ayesha", "Zainab", "Maryam", "Sana", "Rabia", "Nadia", "Saima", "Shazia", "Uzma", "Asma", "Farah",
        "Hira", "Iqra", "Mehwish", "Amna", "Kiran", "Sadia", "Bushra", "Samina", "Tahira", "Sobia", "Mahnoor",
    ],
    "elder": [
        "Naseem Akhtar", "Shamim Bibi", "Razia Begum", "Kausar Parveen", "Zubaida Khatoon", "Nasreen Bibi",
        "Sughra Bibi", "Rasheeda Begum", "Mumtaz Begum", "Hameeda Bibi",
    ],
}
SURNAMES = [
    "Khan", "Ahmed", "Ali", "Hussain", "Malik", "Butt", "Chaudhry", "Qureshi", "Sheikh", "Rana", "Bhatti",
    "Iqbal", "Raza", "Javed", "Mirza", "Akhtar", "Abbasi", "Siddiqui", "Gill", "Cheema", "Awan", "Rehman",
    "Aslam", "Shah", "Anwar", "Mahmood", "Saeed", "Nawaz", "Riaz", "Warraich",
]
# Fathers' / husbands' first names for the "father / husband name" field.
GUARDIAN_FIRST_NAMES = [
    "Muhammad Imran", "Muhammad Asif", "Tariq", "Shahid", "Khalid", "Arshad", "Javed", "Nadeem", "Saleem",
    "Abdul Majeed", "Muhammad Akram", "Zafar", "Iftikhar", "Pervaiz", "Muhammad Riaz", "Ghulam Mustafa",
]

LAHORE_AREAS = [
    "Model Town", "Johar Town", "Gulberg III", "Allama Iqbal Town", "Samanabad", "Garden Town", "Township",
    "Wapda Town", "Shadman", "Faisal Town", "Ichhra", "Sabzazar", "Green Town", "Bahria Town", "Cantt",
]
NEARBY_TOWNS = ["Sheikhupura", "Kasur", "Muridke", "Raiwind", "Gujranwala"]

# Front-desk notes are NON-clinical (language, who comes with the patient, best time to call).
FRONT_DESK_NOTES = [
    "Prefers Punjabi.",
    "Usually comes with a family member.",
    "Hard of hearing - please speak slowly and clearly.",
    "Works in the day - prefers evening appointments.",
    "Son usually books the appointments.",
    "Prefers Urdu messages.",
]

# --- Medical vocabulary ------------------------------------------------------------------------

DIABETES = "Type 2 diabetes"
HYPERTENSION = "Hypertension"
ASTHMA = "Asthma"
HYPOTHYROIDISM = "Hypothyroidism"
IHD = "Ischaemic heart disease"

PENICILLIN = "Penicillin"
SULFA = "Sulfa drugs"
NSAIDS = "NSAIDs"
ALLERGY_CHOICES = [PENICILLIN, SULFA, NSAIDS, f"{PENICILLIN}, {NSAIDS}"]

# key -> (medicine, dose, frequency, duration, instructions). Frequency "1+0+1" = morning + night.
MEDICINES = {
    "panadol": ("Tab. Panadol 500mg", "2 tablets", "1+1+1", "3 days", "For fever or pain, after meals"),
    "augmentin": ("Tab. Augmentin 625mg", "1 tablet", "1+0+1", "5 days", "After meals. Complete the course"),
    "azomax": ("Tab. Azomax 500mg", "1 tablet", "1+0+0", "3 days", "1 hour before a meal"),
    "azomax_7": ("Tab. Azomax 500mg", "1 tablet", "1+0+0", "7 days", "Complete the full course"),
    "rigix": ("Tab. Rigix 10mg", "1 tablet", "0+0+1", "7 days", "At night. May cause drowsiness"),
    "ors": ("ORS sachet", "1 sachet in 1 litre of boiled, cooled water", "After every loose stool",
            "Until stools settle", "Make fresh every 24 hours"),
    "flagyl": ("Tab. Flagyl 400mg", "1 tablet", "1+1+1", "5 days", "After meals"),
    "gravinate": ("Tab. Gravinate 50mg", "1 tablet", "1+0+1", "2 days", "For vomiting"),
    "omeprazole": ("Cap. Omeprazole 20mg", "1 capsule", "1+0+0", "2 weeks", "30 minutes before breakfast"),
    "mucaine": ("Syp. Mucaine", "2 teaspoons", "1+1+1", "1 week", "After meals"),
    "ciproxin": ("Tab. Ciproxin 500mg", "1 tablet", "1+0+1", "5 days", "After meals"),
    "brufen": ("Tab. Brufen 400mg", "1 tablet", "1+0+1", "5 days", "After meals"),
    "myoril": ("Tab. Myoril 4mg", "1 tablet", "1+0+1", "5 days", ""),
    "sunny_d": ("Cap. Sunny D 200,000 IU", "1 capsule", "Once a month", "3 months", "With a glass of milk"),
    "metformin": ("Tab. Metformin 500mg", "1 tablet", "1+0+1", "1 month", "With meals"),
    "getryl": ("Tab. Getryl 2mg", "1 tablet", "1+0+0", "1 month", "15 minutes before breakfast"),
    "amlodipine": ("Tab. Amlodipine 5mg", "1 tablet", "0+0+1", "1 month", ""),
    "losartan": ("Tab. Losartan 50mg", "1 tablet", "1+0+0", "1 month", ""),
    "ventolin": ("Inh. Ventolin 100mcg", "2 puffs", "When needed", "1 month",
                 "For wheeze or breathlessness. Use with a spacer"),
    "symbicort": ("Inh. Symbicort 160/4.5", "1 puff", "1+0+1", "1 month", "Rinse mouth after use"),
    "montika": ("Tab. Montika 10mg", "1 tablet", "0+0+1", "1 month", "At night"),
    "montika_child": ("Tab. Montika 5mg (chewable)", "1 tablet", "0+0+1", "1 month", "At night"),
    "thyroxine": ("Tab. Thyroxine 50mcg", "1 tablet", "1+0+0", "1 month", "Empty stomach, 30 minutes before breakfast"),
    "loprin": ("Tab. Loprin 75mg", "1 tablet", "0+1+0", "1 month", "After lunch"),
    "lowplat": ("Tab. Lowplat 75mg", "1 tablet", "0+1+0", "1 month", "After lunch"),
    "atorvastatin": ("Tab. Atorvastatin 10mg", "1 tablet", "0+0+1", "1 month", "At bedtime"),
    "concor": ("Tab. Concor 2.5mg", "1 tablet", "1+0+0", "1 month", ""),
    # Children's syrups: the dose is worked out from the child's weight (see child_dose).
    "calpol": ("Syp. Calpol 120mg/5ml", "", "1+1+1", "3 days", "For fever, when needed"),
    "amoxil_syrup": ("Syp. Amoxil 250mg/5ml", "", "1+1+1", "5 days", "Shake well. Complete the course"),
    "azomax_syrup": ("Syp. Azomax 200mg/5ml", "", "1+0+0", "3 days", "Shake well"),
    "zincat": ("Syp. Zincat 20mg/5ml", "", "1+0+0", "10 days", ""),
    "ors_child": ("ORS sachet", "1 sachet in 1 litre of boiled, cooled water", "After every loose stool",
                  "Until stools settle", "Give small sips with a spoon"),
}

# Safer alternatives when the patient is allergic (None = leave the medicine out).
ALLERGY_SWAPS = {
    PENICILLIN: {"augmentin": "azomax", "amoxil_syrup": "azomax_syrup"},
    NSAIDS: {"brufen": "panadol", "loprin": "lowplat"},
    SULFA: {"getryl": None},  # sulfonylurea: avoided in sulfa allergy
}


def medicines_for(keys, allergies):
    """The encounter's medicine keys with allergy-unsafe ones swapped or removed (no duplicates)."""
    result = []
    for key in keys:
        for allergy in allergies:
            swaps = ALLERGY_SWAPS.get(allergy, {})
            if key in swaps:
                key = swaps[key]
            if key is None:
                break
        if key is not None and key not in result:
            result.append(key)
    return result


def child_dose(key, weight_kg):
    """Syrup dose in ml for a child of this weight (rounded the way doctors write it)."""

    def ml(value, step=0.5, low=2.5, high=10):
        value = min(max(round(value / step) * step, low), high)
        return f"{value:g} ml"

    if key == "calpol":  # 15 mg/kg per dose of 120 mg/5 ml
        return ml(weight_kg * 15 / 24)
    if key == "amoxil_syrup":  # about 8 mg/kg per dose of 250 mg/5 ml
        return ml(weight_kg * 8 / 50, step=2.5)
    if key == "azomax_syrup":  # 10 mg/kg once a day of 200 mg/5 ml
        return ml(weight_kg * 10 / 40, high=12.5)
    if key == "zincat":
        return "5 ml"
    return ""


# --- Lab results -------------------------------------------------------------------------------

# key -> (test name, [(result text, abnormal?), ...]). The first option of "hba1c" (8.4%) is
# always used for the first HbA1c in the demo so there is a clearly abnormal result to show.
LAB_TESTS = {
    "hba1c": ("HbA1c", [("8.4% (target below 7%)", True), ("7.6%", True), ("6.8%", False), ("9.1%", True)]),
    "fbs": ("Fasting blood sugar", [("156 mg/dL", True), ("132 mg/dL", True), ("98 mg/dL", False)]),
    "lipids": ("Lipid profile", [
        ("Total cholesterol 232 mg/dL, LDL 152 mg/dL, HDL 36 mg/dL, Triglycerides 210 mg/dL", True),
        ("Total cholesterol 178 mg/dL, LDL 98 mg/dL, HDL 44 mg/dL, Triglycerides 140 mg/dL", False),
    ]),
    "creatinine": ("Serum creatinine", [("0.9 mg/dL", False), ("1.1 mg/dL", False)]),
    "tsh": ("TSH", [("6.8 mIU/L", True), ("2.4 mIU/L", False), ("4.1 mIU/L", False)]),
    "ecg": ("ECG", [("Sinus rhythm, rate 66/min. Old inferior Q waves.", True), ("Sinus rhythm. No acute changes.", False)]),
    "cxr": ("Chest X-ray", [("Clear lung fields. No active disease.", False)]),
    "urine": ("Urine R/E", [("Pus cells 20-25/HPF, RBCs 2-4/HPF, nitrites positive", True)]),
    "typhidot": ("Typhidot", [("IgM positive", True)]),
    "cbc": ("CBC", [
        ("Hb 10.2 g/dL, MCV 72 fL, TLC 7,800/µL, Platelets 245,000/µL", True),
        ("Hb 13.4 g/dL, TLC 6,900/µL, Platelets 260,000/µL", False),
    ]),
    "cbc_fever": ("CBC", [("Hb 12.1 g/dL, TLC 4,200/µL, Platelets 165,000/µL", False)]),
    "vitamin_d": ("Vitamin D (25-OH)", [("12 ng/mL (deficient)", True), ("17 ng/mL (insufficient)", True)]),
}

# --- Consultations -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Encounter:
    """A typical consultation. Text fields may be a tuple of variants (one is picked at random)."""

    reason: str  # short reason, used for the appointment booking
    complaint: object
    history: object
    examination: object
    diagnosis: str
    plan: str
    medicines: tuple
    fever: str = ""  # "", "mild" or "high"
    follow_up_days: tuple = None  # (shortest, longest) days until the doctor wants to see them again
    labs: tuple = ()  # (lab key, chance of being ordered)
    condition: str = ""  # the long-term condition this visit reviews
    flare: bool = False  # asthma attack: lower oxygen, faster pulse
    female_only: bool = False
    adult_only: bool = False


ENCOUNTERS = {
    # Short illnesses (adults and teenagers)
    "urti": Encounter(
        reason="Fever and sore throat",
        complaint=("Fever, sore throat and runny nose for 3 days", "Cough, sore throat and body aches for 2 days"),
        history=("Mild dry cough. No shortness of breath. Others at home have a cold.",
                 "No breathing difficulty. Eating and drinking normally."),
        examination="Throat congested, tonsils not enlarged. Chest clear. No enlarged neck glands.",
        diagnosis="Upper respiratory tract infection (viral)",
        plan="Steam inhalation, warm fluids and rest. Antibiotics not needed. Come back if fever lasts more than 3 more days.",
        medicines=("panadol", "rigix"),
        fever="mild",
    ),
    "tonsillitis": Encounter(
        reason="Sore throat",
        complaint="Severe sore throat, fever and pain on swallowing for 2 days",
        history="Fever up to 102°F at home. No cough. Similar episode last winter.",
        examination="Tonsils enlarged with white exudate. Tender neck glands.",
        diagnosis="Acute tonsillitis",
        plan="Complete the full antibiotic course. Warm salt-water gargles. Soft diet.",
        medicines=("augmentin", "panadol"),
        fever="high",
    ),
    "gastroenteritis": Encounter(
        reason="Loose motions",
        complaint="Loose motions and vomiting since yesterday",
        history="6-7 watery stools and 3 vomits. Ate outside two days ago. No blood in stool.",
        examination="Mildly dehydrated. Abdomen soft with mild tenderness all over. Bowel sounds increased.",
        diagnosis="Acute gastroenteritis with mild dehydration",
        plan="ORS after every loose stool. Light diet (khichri, yoghurt, bananas). Come back at once if unable to keep fluids down.",
        medicines=("ors", "flagyl", "gravinate"),
        fever="mild",
    ),
    "gastritis": Encounter(
        reason="Stomach pain",
        complaint=("Burning pain in the upper abdomen after meals for 2 weeks", "Acidity and heartburn for a month"),
        history="Worse with spicy and oily food and tea on an empty stomach. Takes painkillers now and then for headache.",
        examination="Tender in the upper abdomen. No guarding. No pallor.",
        diagnosis="Gastritis / acid peptic disease",
        plan="Avoid painkillers, spicy and oily food and late-night meals. Small, frequent meals.",
        medicines=("omeprazole", "mucaine"),
        follow_up_days=(14, 14),
    ),
    "uti": Encounter(
        reason="Burning urine",
        complaint="Burning on passing urine and going often for 2 days",
        history="Lower abdominal discomfort. No fever, no back pain. Not pregnant.",
        examination="Mild tenderness above the pubic bone. No tenderness over the kidneys.",
        diagnosis="Lower urinary tract infection",
        plan="Drink 8-10 glasses of water a day. Complete the antibiotic course. Urine test sent.",
        medicines=("ciproxin", "panadol"),
        follow_up_days=(7, 10),
        labs=(("urine", 1.0),),
        female_only=True,
        adult_only=True,
    ),
    "back_pain": Encounter(
        reason="Back pain",
        complaint="Lower back pain for 1 week after lifting a heavy load",
        history="Pain does not travel to the legs. No numbness or weakness. No bladder or bowel problems.",
        examination="Tender muscles in the lower back. Straight leg raise normal. Power and sensation normal.",
        diagnosis="Mechanical low back pain",
        plan="Warm compresses. No heavy lifting for 2 weeks. Back exercises as shown. Firm mattress.",
        medicines=("brufen", "myoril"),
        adult_only=True,
    ),
    "typhoid": Encounter(
        reason="Fever for a week",
        complaint="Fever for 7 days with weakness and loss of appetite",
        history="Fever rising each day, worse in the evening. Mild abdominal discomfort. Drinks water from outside.",
        examination="Temperature raised. Coated tongue. Mild abdominal tenderness. No rash.",
        diagnosis="Enteric fever (typhoid)",
        plan="Complete the full 7-day course even if the fever settles. Boiled water only. Light diet.",
        medicines=("azomax_7", "panadol"),
        fever="high",
        follow_up_days=(7, 7),
        labs=(("typhidot", 1.0), ("cbc_fever", 0.7)),
    ),
    "allergic_rhinitis": Encounter(
        reason="Sneezing and blocked nose",
        complaint="Sneezing, itchy eyes and blocked nose for 2 weeks",
        history="Worse in the mornings and with dust. Similar episodes every spring.",
        examination="Pale, swollen nasal lining. Chest clear.",
        diagnosis="Allergic rhinitis",
        plan="Avoid dust and smoke. Cover nose and mouth outdoors. Wash bed sheets weekly.",
        medicines=("rigix",),
    ),
    "vitamin_d": Encounter(
        reason="Tiredness and body aches",
        complaint="Tiredness and body aches for 2 months",
        history="Aches in legs and back. Spends little time outdoors. Little milk or yoghurt in diet.",
        examination="Looks well. Mild pallor. Mild tenderness over the shins.",
        diagnosis="Vitamin D deficiency",
        plan="15-20 minutes of morning sunlight daily. Milk, yoghurt and eggs in the diet.",
        medicines=("sunny_d",),
        follow_up_days=(28, 42),
        labs=(("vitamin_d", 1.0), ("cbc", 0.6)),
        adult_only=True,
    ),
    # Children
    "child_fever": Encounter(
        reason="Fever",
        complaint=("Fever and cough for 2 days", "Fever and runny nose since yesterday"),
        history="Eating less than usual. No fits, no rash. Vaccinations up to date as per mother.",
        examination="Active and alert. Throat red. Chest clear. Ears normal.",
        diagnosis="Viral fever / upper respiratory tract infection",
        plan="Sponge with lukewarm water if the fever is high. Plenty of fluids. Bring back at once if breathing fast, very sleepy or has a fit.",
        medicines=("calpol",),
        fever="high",
    ),
    "child_diarrhoea": Encounter(
        reason="Loose motions",
        complaint="Loose motions for 2 days",
        history="5-6 watery stools a day, no blood. Still feeding well.",
        examination="Alert, drinks eagerly. Skin pinch goes back quickly. Abdomen soft.",
        diagnosis="Acute watery diarrhoea without dehydration",
        plan="ORS after every loose stool. Continue normal feeding. Zinc for 10 days.",
        medicines=("ors_child", "zincat"),
    ),
    "child_tonsillitis": Encounter(
        reason="Fever and sore throat",
        complaint="Fever and sore throat for 3 days",
        history="Not eating well because of throat pain. No cough.",
        examination="Tonsils enlarged and red with white spots. Tender neck glands.",
        diagnosis="Acute tonsillitis",
        plan="Complete the full antibiotic course. Soft food and plenty of fluids.",
        medicines=("amoxil_syrup", "calpol"),
        fever="high",
    ),
    # Long-term condition reviews
    "diabetes_review": Encounter(
        reason="Sugar check-up",
        complaint="Routine diabetes check-up",
        history=("Taking medicines regularly. Occasional increased thirst. No numbness in the feet.",
                 "Missed medicines for a few days last month. Eats rice daily. No foot problems."),
        examination="Feet examined: no ulcers, pulses felt, sensation normal.",
        diagnosis="Type 2 diabetes mellitus - review",
        plan="Cut down sugar, sweets and white rice. Walk 30 minutes daily. HbA1c every 3 months.",
        medicines=("metformin", "getryl"),
        follow_up_days=(28, 42),
        labs=(("hba1c", 0.5), ("fbs", 0.2)),
        condition=DIABETES,
    ),
    "hypertension_review": Encounter(
        reason="BP check-up",
        complaint="Blood pressure check-up",
        history=("Occasional morning headache. Taking medicine regularly. No chest pain.",
                 "Home BP readings around 140/90. No breathlessness or ankle swelling."),
        examination="Heart sounds normal. No ankle swelling.",
        diagnosis="Essential hypertension - review",
        plan="Less salt: no extra salt on food, fewer pickles and packaged snacks. Daily walk. Note home BP readings.",
        medicines=("amlodipine",),  # swapped for losartan for some patients (see seed_demo)
        follow_up_days=(28, 42),
        labs=(("lipids", 0.3), ("creatinine", 0.2)),
        condition=HYPERTENSION,
    ),
    "asthma_review": Encounter(
        reason="Asthma follow-up",
        complaint="Wheeze and cough at night for 1 week",
        history="Needs the reliever inhaler 3-4 times a week. Triggered by dust and cold weather. No fever.",
        examination="Mild wheeze on both sides of the chest. No distress.",
        diagnosis="Bronchial asthma - mild flare",
        plan="Use the preventer inhaler every day, even when well. Avoid dust and smoke.",
        medicines=("ventolin", "symbicort", "montika"),
        follow_up_days=(28, 42),
        labs=(("cxr", 0.15),),
        condition=ASTHMA,
        flare=True,
    ),
    "thyroid_review": Encounter(
        reason="Thyroid follow-up",
        complaint="Thyroid follow-up",
        history="Less tired since starting medicine. Weight stable. Periods regular.",
        examination="No neck swelling. Pulse regular.",
        diagnosis="Hypothyroidism - review",
        plan="Take thyroxine on an empty stomach, 30 minutes before breakfast. Repeat TSH in 6 weeks.",
        medicines=("thyroxine",),
        follow_up_days=(42, 56),
        labs=(("tsh", 0.6),),
        condition=HYPOTHYROIDISM,
    ),
    "heart_review": Encounter(
        reason="Heart follow-up",
        complaint="Heart follow-up; chest tightness when climbing stairs",
        history="Tightness settles with rest within 5 minutes. No pain at rest. Taking medicines regularly.",
        examination="Heart sounds normal. Chest clear. No ankle swelling.",
        diagnosis="Ischaemic heart disease - stable angina",
        plan="Go to hospital emergency at once if chest pain lasts more than 10 minutes or comes at rest. Daily slow walk. No smoking.",
        medicines=("loprin", "atorvastatin", "concor"),
        follow_up_days=(28, 42),
        labs=(("lipids", 0.4), ("ecg", 0.3)),
        condition=IHD,
    ),
}

ADULT_ACUTE = ["urti", "tonsillitis", "gastroenteritis", "gastritis", "uti", "back_pain", "typhoid",
               "allergic_rhinitis", "vitamin_d"]
CHILD_ACUTE = ["child_fever", "child_diarrhoea", "child_tonsillitis"]
REVIEW_FOR = {
    DIABETES: "diabetes_review",
    HYPERTENSION: "hypertension_review",
    ASTHMA: "asthma_review",
    HYPOTHYROIDISM: "thyroid_review",
    IHD: "heart_review",
}


def pick(rng, value):
    """A text field may be a single string or a tuple of variants."""
    return rng.choice(value) if isinstance(value, tuple) else value


def acute_choices(age, sex):
    """Short-illness encounters that suit this patient."""
    if age < 13:
        return CHILD_ACUTE
    return [
        key for key in ADULT_ACUTE
        if not (ENCOUNTERS[key].female_only and sex != "F") and not (ENCOUNTERS[key].adult_only and age < 18)
    ]


# --- Vital signs -------------------------------------------------------------------------------

def _decimal(value):
    return Decimal(f"{value:.1f}")


def vitals_for(rng, *, age, conditions, weight, height, encounter, first_visit):
    """Vital signs that fit the patient's age, long-term conditions and today's illness."""
    child = age < 13
    vitals = {}

    if child or rng.random() < 0.75:
        vitals["weight_kg"] = _decimal(max(weight + rng.uniform(-1.2, 1.2), 3))
    if first_visit:
        vitals["height_cm"] = _decimal(height)

    # Temperature
    if encounter.fever == "high":
        vitals["temperature_c"] = _decimal(rng.uniform(38.3, 39.4))
    elif encounter.fever == "mild":
        vitals["temperature_c"] = _decimal(rng.uniform(37.6, 38.1))
    elif rng.random() < 0.5:
        vitals["temperature_c"] = _decimal(rng.uniform(36.7, 37.1))

    # Pulse
    if child:
        pulse = rng.randint(90, 118)
    elif IHD in conditions:
        pulse = rng.randint(58, 70)  # on a beta-blocker
    else:
        pulse = rng.randint(68, 90)
    if encounter.fever:
        pulse += rng.randint(8, 16)
    if encounter.flare:
        pulse += rng.randint(6, 12)
    vitals["pulse"] = pulse

    # Blood pressure (adults)
    if not child:
        if HYPERTENSION in conditions:
            systolic, diastolic = rng.randint(136, 164), rng.randint(86, 100)
        elif IHD in conditions:
            systolic, diastolic = rng.randint(118, 138), rng.randint(74, 88)
        else:
            systolic, diastolic = rng.randint(108, 130), rng.randint(70, 84)
            if age >= 65:
                systolic += rng.randint(4, 10)
        vitals["bp_systolic"], vitals["bp_diastolic"] = systolic, diastolic

    # Oxygen
    if encounter.flare:
        vitals["spo2"] = rng.randint(93, 96)
    elif encounter.fever or rng.random() < 0.35:
        vitals["spo2"] = rng.randint(96, 99)

    # Blood sugar (random, mg/dL)
    if DIABETES in conditions and (encounter.condition == DIABETES or rng.random() < 0.5):
        vitals["blood_sugar"] = rng.randint(145, 265)
    elif not child and age >= 40 and rng.random() < 0.15:
        vitals["blood_sugar"] = rng.randint(92, 128)

    return vitals
