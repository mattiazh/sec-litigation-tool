import re
import sqlite3
from datetime import datetime
from pathlib import Path

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None


EXCEL_CELL_CHARACTER_LIMIT = 32767


def limit_excel_text(value):
    """Keeps text within Excel's maximum number of characters per cell."""

    text = value or ""

    if len(text) <= EXCEL_CELL_CHARACTER_LIMIT:
        return text

    ending = "\n[Text truncated because of Excel's cell limit.]"
    return text[: EXCEL_CELL_CHARACTER_LIMIT - len(ending)] + ending


def connect_database(database_file):
    """Opens the SQLite database with named-column rows."""

    connection = sqlite3.connect(database_file)
    connection.row_factory = sqlite3.Row
    return connection


def load_release_rows(database_file, start_number, end_number):
    """Loads the selected release interval in descending order."""

    with connect_database(database_file) as connection:
        rows = connection.execute(
            """
            SELECT
                date,
                respondents,
                release_no,
                keyword,
                content,
                complaint,
                link_to_release,
                link_to_complaint
            FROM releases
            WHERE CAST(SUBSTR(release_no, 4) AS INTEGER)
                  BETWEEN ? AND ?
            ORDER BY CAST(SUBSTR(release_no, 4) AS INTEGER) DESC
            """,
            (end_number, start_number),
        ).fetchall()

    return [dict(row) for row in rows]


def load_document_rows(database_file, start_number, end_number):
    """Loads every stored Resource document for the selected interval."""

    with connect_database(database_file) as connection:
        rows = connection.execute(
            """
            SELECT
                release_no,
                document_name,
                document_url,
                page_count,
                character_count,
                manual_review_required,
                review_reason
            FROM documents
            WHERE CAST(SUBSTR(release_no, 4) AS INTEGER)
                  BETWEEN ? AND ?
            ORDER BY
                CAST(SUBSTR(release_no, 4) AS INTEGER) DESC,
                id ASC
            """,
            (end_number, start_number),
        ).fetchall()

    return [dict(row) for row in rows]


def write_summary_with_bold_headings(
    worksheet,
    row,
    column,
    summary_text,
    normal_format,
    bold_format,
):
    """Writes **marked** summary headings as genuine bold text in Excel."""

    summary_text = limit_excel_text(summary_text)

    if not summary_text:
        worksheet.write_blank(row, column, None, normal_format)
        return

    pieces = re.split(r"(\*\*.*?\*\*)", summary_text, flags=re.DOTALL)
    rich_parts = []
    contains_bold_text = False

    for piece in pieces:
        if not piece:
            continue

        if piece.startswith("**") and piece.endswith("**"):
            heading_text = piece[2:-2]

            if heading_text:
                rich_parts.extend([bold_format, heading_text])
                contains_bold_text = True
        else:
            rich_parts.append(piece)

    if contains_bold_text and len(rich_parts) >= 3:
        result = worksheet.write_rich_string(
            row,
            column,
            *rich_parts,
            normal_format,
        )

        if result == 0:
            return

    worksheet.write_string(
        row,
        column,
        summary_text.replace("**", ""),
        normal_format,
    )


def export_interval_to_excel(
    database_file,
    export_folder,
    start_number,
    end_number,
):
    """Creates a formatted Excel report for the chosen release interval."""

    print(f"Using Excel exporter v3 from: {Path(__file__).resolve()}")

    if xlsxwriter is None:
        raise RuntimeError(
            "The XlsxWriter package is missing. Install it with: "
            "python -m pip install XlsxWriter"
        )

    database_file = Path(database_file)
    export_folder = Path(export_folder)

    release_rows = load_release_rows(
        database_file,
        start_number,
        end_number,
    )

    if not release_rows:
        raise ValueError(
            "No stored releases were found for the selected interval."
        )

    document_rows = load_document_rows(
        database_file,
        start_number,
        end_number,
    )

    # Group the Resource documents by release while preserving the order in
    # which they appeared on the SEC page and were stored in SQLite.
    documents_by_release = {}

    for document in document_rows:
        documents_by_release.setdefault(
            document["release_no"],
            [],
        ).append(document)

    maximum_document_count = max(
        (len(documents) for documents in documents_by_release.values()),
        default=0,
    )

    export_folder.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    export_path = export_folder / (
        f"SEC_Litigation_Releases_"
        f"LR-{start_number}_to_LR-{end_number}_{timestamp}.xlsx"
    )

    workbook = xlsxwriter.Workbook(export_path)

    header_format = workbook.add_format(
        {
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#1F4E78",
            "border": 1,
            "align": "center",
            "valign": "vcenter",
        }
    )
    text_format = workbook.add_format(
        {
            "text_wrap": True,
            "valign": "top",
            "border": 1,
            "font_size": 10,
        }
    )
    date_format = workbook.add_format(
        {
            "num_format": "dd.mm.yyyy",
            "valign": "top",
            "border": 1,
            "font_size": 10,
        }
    )
    hyperlink_format = workbook.add_format(
        {
            "font_color": "#0563C1",
            "underline": True,
            "text_wrap": True,
            "valign": "top",
            "border": 1,
            "font_size": 10,
        }
    )
    bold_inline_format = workbook.add_format(
        {
            "bold": True,
            "font_size": 10,
        }
    )
    missing_format = workbook.add_format(
        {
            "bg_color": "#FFF2CC",
        }
    )
    manual_review_format = workbook.add_format(
        {
            "bg_color": "#F4CCCC",
        }
    )

    worksheet = workbook.add_worksheet("Litigation Releases")
    worksheet.hide_gridlines(2)
    worksheet.freeze_panes(1, 0)
    worksheet.set_landscape()
    worksheet.fit_to_pages(1, 0)
    worksheet.repeat_rows(0)
    worksheet.set_margins(0.25, 0.25, 0.5, 0.5)
    worksheet.set_header("&CSEC Litigation Releases")
    worksheet.set_footer(
        f"&LGenerated {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        "&RPage &P of &N"
    )

    headers = [
        "Date",
        "Respondents",
        "Release No.",
        "Keyword",
        "Content",
        "Complaint",
        "Link to Release",
        "Link to Complaint",
    ]

    # The first Resource document stays in the existing Link to Complaint
    # column. Every further Resource document receives its own column.
    for document_number in range(2, maximum_document_count + 1):
        headers.append(f"Additional Resource {document_number}")

    worksheet.write_row(0, 0, headers, header_format)
    worksheet.set_row(0, 28)

    worksheet.set_column("A:A", 12)
    worksheet.set_column("B:B", 38)
    worksheet.set_column("C:C", 13)
    worksheet.set_column("D:D", 34)
    worksheet.set_column("E:F", 75)
    worksheet.set_column("G:G", 27)

    if maximum_document_count > 0:
        worksheet.set_column(
            7,
            7 + maximum_document_count - 1,
            42,
        )
    else:
        worksheet.set_column("H:H", 42)

    for excel_row, release in enumerate(release_rows, start=1):
        date_text = release["date"] or ""

        try:
            date_value = datetime.strptime(date_text, "%d.%m.%Y")
            worksheet.write_datetime(
                excel_row,
                0,
                date_value,
                date_format,
            )
        except ValueError:
            worksheet.write_string(
                excel_row,
                0,
                date_text,
                text_format,
            )

        worksheet.write_string(
            excel_row,
            1,
            limit_excel_text(release["respondents"]),
            text_format,
        )
        worksheet.write_string(
            excel_row,
            2,
            release["release_no"] or "",
            text_format,
        )
        worksheet.write_string(
            excel_row,
            3,
            limit_excel_text(release["keyword"]),
            text_format,
        )
        worksheet.write_string(
            excel_row,
            4,
            limit_excel_text(release["content"]),
            text_format,
        )

        write_summary_with_bold_headings(
            worksheet,
            excel_row,
            5,
            release["complaint"] or "",
            text_format,
            bold_inline_format,
        )

        release_url = release["link_to_release"] or ""

        if release_url:
            worksheet.write_url(
                excel_row,
                6,
                release_url,
                hyperlink_format,
                string="Open SEC release",
            )
        else:
            worksheet.write_blank(
                excel_row,
                6,
                None,
                text_format,
            )

        release_documents = documents_by_release.get(
            release["release_no"],
            [],
        )

        # Write one Resource document per cell. The clickable display text is
        # exactly the document label extracted from the SEC Resources section.
        for document_index in range(maximum_document_count):
            column_index = 7 + document_index

            if document_index < len(release_documents):
                document = release_documents[document_index]
                document_url = document["document_url"] or ""
                document_name = (
                    document["document_name"]
                    or "Open Resource document"
                )

                if document_url:
                    worksheet.write_url(
                        excel_row,
                        column_index,
                        document_url,
                        hyperlink_format,
                        string=limit_excel_text(document_name),
                    )
                else:
                    worksheet.write_string(
                        excel_row,
                        column_index,
                        limit_excel_text(document_name),
                        text_format,
                    )
            else:
                worksheet.write_blank(
                    excel_row,
                    column_index,
                    None,
                    text_format,
                )

        # Keep the original Link to Complaint column visible even when the
        # selected interval contains no Resource documents at all.
        if maximum_document_count == 0:
            worksheet.write_blank(
                excel_row,
                7,
                None,
                text_format,
            )

        worksheet.set_row(excel_row, 115)

    last_release_row = len(release_rows)
    worksheet.autofilter(0, 0, last_release_row, len(headers) - 1)

    worksheet.conditional_format(
        1,
        3,
        last_release_row,
        3,
        {
            "type": "blanks",
            "format": missing_format,
        },
    )
    worksheet.conditional_format(
        1,
        5,
        last_release_row,
        5,
        {
            "type": "blanks",
            "format": missing_format,
        },
    )

    if document_rows:
        documents_sheet = workbook.add_worksheet("Resource Documents")
        documents_sheet.hide_gridlines(2)
        documents_sheet.freeze_panes(1, 0)

        document_headers = [
            "Release No.",
            "Document Name",
            "Document URL",
            "Pages",
            "Extracted Characters",
            "Manual Review",
            "Review Reason",
        ]
        documents_sheet.write_row(
            0,
            0,
            document_headers,
            header_format,
        )
        documents_sheet.set_row(0, 28)
        documents_sheet.set_column("A:A", 13)
        documents_sheet.set_column("B:B", 42)
        documents_sheet.set_column("C:C", 65)
        documents_sheet.set_column("D:E", 18)
        documents_sheet.set_column("F:F", 15)
        documents_sheet.set_column("G:G", 48)

        for excel_row, document in enumerate(document_rows, start=1):
            documents_sheet.write_string(
                excel_row,
                0,
                document["release_no"] or "",
                text_format,
            )
            documents_sheet.write_string(
                excel_row,
                1,
                limit_excel_text(document["document_name"]),
                text_format,
            )

            document_url = document["document_url"] or ""

            if document_url:
                documents_sheet.write_url(
                    excel_row,
                    2,
                    document_url,
                    hyperlink_format,
                    string=document_url,
                )
            else:
                documents_sheet.write_blank(
                    excel_row,
                    2,
                    None,
                    text_format,
                )

            documents_sheet.write_number(
                excel_row,
                3,
                document["page_count"] or 0,
                text_format,
            )
            documents_sheet.write_number(
                excel_row,
                4,
                document["character_count"] or 0,
                text_format,
            )

            manual_review = bool(document["manual_review_required"])
            documents_sheet.write_string(
                excel_row,
                5,
                "Yes" if manual_review else "No",
                text_format,
            )
            documents_sheet.write_string(
                excel_row,
                6,
                limit_excel_text(document["review_reason"]),
                text_format,
            )
            documents_sheet.set_row(excel_row, 40)

        last_document_row = len(document_rows)
        documents_sheet.autofilter(
            0,
            0,
            last_document_row,
            len(document_headers) - 1,
        )
        documents_sheet.conditional_format(
            1,
            5,
            last_document_row,
            5,
            {
                "type": "text",
                "criteria": "containing",
                "value": "Yes",
                "format": manual_review_format,
            },
        )

    workbook.close()

    return export_path, len(release_rows), len(document_rows)