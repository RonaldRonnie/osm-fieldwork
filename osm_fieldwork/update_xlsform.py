"""Update an existing XLSForm with additional fields useful for field mapping."""

import logging
import re
from datetime import datetime
from io import BytesIO
from uuid import uuid4

import pandas as pd
from python_calamine.pandas import pandas_monkeypatch

from osm_fieldwork.form_components.choice_fields import choices_df, digitisation_choices_df
from osm_fieldwork.form_components.digitisation_fields import digitisation_df
from osm_fieldwork.form_components.mandatory_fields import entities_df, meta_df, settings_df, survey_df

log = logging.getLogger(__name__)

# Monkeypatch pandas to add calamine driver
pandas_monkeypatch()

# Constants
FEATURE_COLUMN = "feature"
NAME_COLUMN = "name"
TYPE_COLUMN = "type"
SURVEY_GROUP_NAME = "survey_questions"
DEFAULT_LANGUAGES = {
    "english": "en",
    "french": "fr",
    "spanish": "es",
    "swahili": "sw",
    "nepali": "ne",
}

# def handle_translations(
#     mandatory_df: pd.DataFrame, user_question_df: pd.DataFrame, digitisation_df: pd.DataFrame, fields: list[str]
# ):
#     """Handle translations, defaulting to English if no translations are present.

#     Handles all field types that can be translated, such as
#     'label', 'hint', 'required_message'.
#     """
#     for field in fields:
#         # Identify translation columns for this field in the user_question_df
#         translation_columns = [col for col in user_question_df.columns if col.startswith(f"{field}::")]

#         if field in user_question_df.columns and not translation_columns:
#             # If user_question_df has only the base field (e.g., 'label'), map English translation from mandatory and digitisation
#             mandatory_df[field] = mandatory_df.get(f"{field}::english(en)", mandatory_df.get(field))
#             digitisation_df[field] = digitisation_df.get(f"{field}::english(en)", digitisation_df.get(field))

#             # Then drop translation columns
#             mandatory_df = mandatory_df.loc[:, ~mandatory_df.columns.str.startswith("label::")]
#             digitisation_df = digitisation_df.loc[:, ~digitisation_df.columns.str.startswith("label::")]

#         else:
#             # If translation columns exist, match them for mandatory and digitisation dataframes
#             for col in translation_columns:
#                 mandatory_col = mandatory_df.get(col)
#                 digitisation_col = digitisation_df.get(col)
#                 if mandatory_col is not None:
#                     mandatory_df[col] = mandatory_col
#                 if digitisation_col is not None:
#                     digitisation_df[col] = digitisation_col

#     return mandatory_df, user_question_df, digitisation_df


def standardize_xlsform_sheets(xlsform: dict) -> dict:
    """Standardizes column headers in both the 'survey' and 'choices' sheets of an XLSForm.

    - Strips spaces and lowercases all column headers.
    - Fixes formatting for columns with '::' (e.g., multilingual labels).

    Args:
        xlsform (dict): A dictionary with keys 'survey' and 'choices', each containing a DataFrame.

    Returns:
        dict: The updated XLSForm dictionary with standardized column headers.
    """

    def standardize_language_columns(df):
        """Standardize existing language columns.

        :param df: Original DataFrame with existing translations.
        :param DEFAULT_LANGAUGES: List of DEFAULT_LANGAUGES with their short codes, e.g., {"english": "en", "french": "fr"}.
        :param base_columns: List of base columns to check (e.g., 'label', 'hint', 'required_message').
        :return: Updated DataFrame with standardized and complete language columns.
        """
        base_columns = ["label", "hint", "required_message"]
        df.columns = df.columns.str.lower()
        existing_columns = df.columns.tolist()

        # Map existing columns and standardize their names
        for col in existing_columns:
            standardized_col = col
            for base_col in base_columns:
                if col.startswith(f"{base_col}::"):
                    match = re.match(rf"{base_col}::(\w+)", col)
                    if match:
                        lang_name = match.group(1)
                        if lang_name in DEFAULT_LANGUAGES:
                            standardized_col = f"{base_col}::{lang_name}({DEFAULT_LANGUAGES[lang_name]})"

                elif col == base_col:  # if only label,hint or required_message then add '::english(en)'
                    standardized_col = f"{base_col}::english(en)"

                if col != standardized_col:
                    df.rename(columns={col: standardized_col}, inplace=True)
        return df

    def filter_df_empty_rows(df: pd.DataFrame, column: str = NAME_COLUMN):
        """Remove rows with None values in the specified column.

        NOTE We retain 'end group' and 'end group' rows even if they have no name.
        NOTE A generic df.dropna(how="all") would not catch accidental spaces etc.
        """
        if column in df.columns:
            # Only retain 'begin group' and 'end group' if 'type' column exists
            if "type" in df.columns:
                return df[(df[column].notna()) | (df["type"].isin(["begin group", "end group", "begin_group", "end_group"]))]
            else:
                return df[df[column].notna()]
        return df

    for sheet_name, sheet_df in xlsform.items():
        if sheet_df.empty:
            continue
        # standardize the language columns
        sheet_df = standardize_language_columns(sheet_df)
        sheet_df = filter_df_empty_rows(sheet_df)
        xlsform[sheet_name] = sheet_df

    return xlsform


def create_survey_group(name: str) -> dict[str, pd.DataFrame]:
    """Helper function to create a begin and end group for XLSForm."""
    begin_group = pd.DataFrame(
        {
            "type": ["begin group"],
            "name": [name],
            "label::english(en)": [name],
            "label::swahili(sw)": [name],
            "label::french(fr)": [name],
            "label::spanish(es)": [name],
            "relevant": "(${new_feature} != '') or (${building_exists} = 'yes')",
        }
    )
    end_group = pd.DataFrame(
        {
            "type": ["end group"],
        }
    )
    return {"begin": begin_group, "end": end_group}


def normalize_with_meta(row, meta_df):
    """Replace metadata in user_question_df with metadata from meta_df of mandatory fields if exists."""
    matching_meta = meta_df[meta_df["type"] == row[TYPE_COLUMN]]
    if not matching_meta.empty:
        for col in matching_meta.columns:
            row[col] = matching_meta.iloc[0][col]
    return row


def merge_dataframes(mandatory_df: pd.DataFrame, user_question_df: pd.DataFrame, digitisation_df: pd.DataFrame) -> pd.DataFrame:
    """Merge multiple Pandas dataframes together, removing duplicate fields."""
    if "list_name" in user_question_df.columns:
        merged_df = pd.concat(
            [
                mandatory_df,
                user_question_df,
                digitisation_df,
            ],
            ignore_index=True,
        )
        # NOTE here we remove duplicate PAIRS based on `list_name` and the name column
        # we have `allow_duplicate_choices` set in the settings sheet, so it is possible
        # to have duplicate NAME_COLUMN entries, if they are in different a `list_name`.
        return merged_df.drop_duplicates(subset=["list_name", NAME_COLUMN], ignore_index=True)

    user_question_df = user_question_df.apply(normalize_with_meta, axis=1, meta_df=meta_df)

    # Find common fields between user_question_df and mandatory_df or digitisation_df
    duplicate_fields = set(user_question_df[NAME_COLUMN]).intersection(
        set(mandatory_df[NAME_COLUMN]).union(set(digitisation_df[NAME_COLUMN]))
    )

    # NOTE filter out 'end group' from duplicate check as they have empty NAME_COLUMN
    end_group_rows = user_question_df[user_question_df["type"].isin(["end group", "end_group"])]
    user_question_df_filtered = user_question_df[
        (~user_question_df[NAME_COLUMN].isin(duplicate_fields)) | (user_question_df.index.isin(end_group_rows.index))
    ]

    # Create survey group wrapper
    survey_group = create_survey_group(SURVEY_GROUP_NAME)

    # Concatenate dataframes in the desired order
    return pd.concat(
        [
            mandatory_df,
            # Wrap the survey question in a group
            survey_group["begin"],
            user_question_df_filtered,
            survey_group["end"],
            digitisation_df,
        ],
        ignore_index=True,
    )


def append_select_one_from_file_row(df: pd.DataFrame, entity_name: str) -> pd.DataFrame:
    """Add a new select_one_from_file question to reference an Entity."""
    # Find the row index where name column = 'feature'
    select_one_from_file_index = df.index[df[NAME_COLUMN] == FEATURE_COLUMN].tolist()
    if not select_one_from_file_index:
        raise ValueError(f"Row with '{NAME_COLUMN}' == '{FEATURE_COLUMN}' not found in survey sheet.")

    # Find the row index after 'feature' row
    row_index_to_split_on = select_one_from_file_index[0] + 1

    additional_row = pd.DataFrame(
        {
            "type": [f"select_one_from_file {entity_name}.csv"],
            "name": [entity_name],
            "label::english(en)": [entity_name],
            "appearance": ["map"],
            "label::swahili(sw)": [entity_name],
            "label::french(fr)": [entity_name],
            "label::spanish(es)": [entity_name],
        }
    )

    # Prepare the row for calculating coordinates based on the additional entity
    coordinates_row = pd.DataFrame(
        {
            "type": ["calculate"],
            "name": ["additional_geometry"],
            "calculation": [f"instance('{entity_name}')/root/item[name=${{{entity_name}}}]/geometry"],
            "label::english(en)": ["additional_geometry"],
            "label::swahili(sw)": ["additional_geometry"],
            "label::french(fr)": ["additional_geometry"],
            "label::spanish(es)": ["additional_geometry"],
        }
    )
    # Insert the new row into the DataFrame
    top_df = df.iloc[:row_index_to_split_on]
    bottom_df = df.iloc[row_index_to_split_on:]
    return pd.concat([top_df, additional_row, coordinates_row, bottom_df], ignore_index=True)


async def append_mandatory_fields(
    custom_form: BytesIO,
    form_category: str,
    additional_entities: list[str] = None,
    existing_id: str = None,
) -> tuple[str, BytesIO]:
    """Append mandatory fields to the XLSForm for use in FMTM.

    Args:
        custom_form(BytesIO): the XLSForm data uploaded, wrapped in BytesIO.
        form_category(str): the form category name (in form_title and description).
        additional_entities(list[str]): add extra select_one_from_file fields to
            reference an additional Entity list (set of geometries).
            The values should be plural, so that 's' will be stripped in the
            field name.
        existing_id(str): an existing UUID to use for the form_id, else random uuid4.

    Returns:
        tuple(str, BytesIO): the xFormId + the update XLSForm wrapped in BytesIO.
    """
    log.info("Appending field mapping questions to XLSForm")
    custom_sheets = pd.read_excel(custom_form, sheet_name=None, engine="calamine")

    if "survey" not in custom_sheets:
        msg = "Survey sheet is required in XLSForm!"
        log.error(msg)
        raise ValueError(msg)

    custom_sheets = standardize_xlsform_sheets(custom_sheets)

    log.debug("Merging survey sheet XLSForm data")
    custom_sheets["survey"] = merge_dataframes(survey_df, custom_sheets.get("survey"), digitisation_df)
    # Hardcode the form_category value for the start instructions
    if form_category.endswith("s"):
        # Plural to singular
        form_category = form_category[:-1]
    form_category_row = custom_sheets["survey"].loc[custom_sheets["survey"]["name"] == "form_category"]
    if not form_category_row.empty:
        custom_sheets["survey"].loc[custom_sheets["survey"]["name"] == "form_category", "calculation"] = f"once('{form_category}')"

    # Ensure the 'choices' sheet exists in custom_sheets
    if "choices" not in custom_sheets or custom_sheets["choices"] is None:
        custom_sheets["choices"] = pd.DataFrame(columns=["list_name", "name", "label::english(en)"])

    log.debug("Merging choices sheet XLSForm data")
    custom_sheets["choices"] = merge_dataframes(choices_df, custom_sheets.get("choices"), digitisation_choices_df)

    # Append or overwrite 'entities' and 'settings' sheets
    log.debug("Overwriting entities and settings XLSForm sheets")
    custom_sheets["entities"] = entities_df
    custom_sheets["settings"] = settings_df
    if "entities" not in custom_sheets:
        msg = "Entities sheet is required in XLSForm!"
        log.error(msg)
        raise ValueError(msg)
    if "settings" not in custom_sheets:
        msg = "Settings sheet is required in XLSForm!"
        log.error(msg)
        raise ValueError(msg)

    # Set the 'version' column to the current timestamp (if 'version' column exists in 'settings')
    xform_id = existing_id if existing_id else uuid4()
    current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.debug(f"Setting xFormId = {xform_id} | form title = {form_category} | version = {current_datetime}")
    custom_sheets["settings"]["version"] = current_datetime
    custom_sheets["settings"]["form_id"] = xform_id
    custom_sheets["settings"]["form_title"] = form_category

    # Append select_one_from_file for additional entities
    if additional_entities:
        log.debug("Adding additional entity list reference to XLSForm")
        for entity_name in additional_entities:
            custom_sheets["survey"] = append_select_one_from_file_row(custom_sheets["survey"], entity_name)

    # Return spreadsheet wrapped as BytesIO memory object
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in custom_sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
    output.seek(0)
    return (xform_id, output)
