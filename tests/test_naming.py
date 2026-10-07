import unittest

from helm.naming import disambiguate, name_from, task_name, ticket_of, title_of


class TheTrackerIdIsTheNameTests(unittest.TestCase):
    def test_the_recorded_ticket_wins(self):
        task = {"ticket": "TICKET-123", "brief": "Something else entirely"}
        self.assertEqual(task_name(task), "TICKET-123")

    def test_a_ticket_in_the_brief_is_found_when_the_field_is_empty(self):
        # Tasks created before --ticket was passed, or by a caller that put the
        # id only in the prose, still get named rather than falling back to a
        # generated key.
        task = {"brief": "TICKET-456 -- clear the upload gate on the stop branch"}
        self.assertEqual(task_name(task), "TICKET-456")

    def test_a_ticket_after_the_first_line_is_found_when_it_is_the_only_one(self):
        # A request often says what it is about after a sentence of context.
        # Reading only the first line left that work, and the lead appointed
        # for it, named by a generated id.
        task = {"brief": "The mic goes silent after a minute.\n\nThis is TICKET-1052."}
        self.assertEqual(ticket_of(task), "TICKET-1052")
        long_line = {"brief": ("context " * 40) + "for TICKET-7"}
        self.assertEqual(ticket_of(long_line), "TICKET-7")

    def test_two_different_tickets_name_neither(self):
        # Later prose mentions other work; picking whichever came first would
        # name a task after something it merely referenced.
        task = {"brief": "Resolve the merge conflicts\n\nRelated: OTHER-999 and OTHER-1000"}
        self.assertEqual(ticket_of(task), "")
        self.assertNotIn(task_name(task), {"OTHER-999", "OTHER-1000"})
        both = {"brief": "TICKET-1 and TICKET-2 together"}
        self.assertEqual(ticket_of(both), "")

    def test_the_same_ticket_repeated_is_still_one(self):
        task = {"brief": "Follow up\nTICKET-5 is blocked; see TICKET-5 notes"}
        self.assertEqual(ticket_of(task), "TICKET-5")

    def test_the_opening_line_names_the_work_over_a_later_reference(self):
        task = {"brief": "TICKET-1: fix the export\n\nDepends on TICKET-2 landing."}
        self.assertEqual(ticket_of(task), "TICKET-1")

    def test_a_drivers_role_document_never_names_it(self):
        task = {"role": "foreman", "brief": "You are a task lead.\n\nSee TICKET-9 in the record."}
        self.assertEqual(ticket_of(task), "")

    def test_the_field_is_trusted_over_a_different_id_in_the_prose(self):
        task = {"ticket": "TICKET-1", "brief": "TICKET-2 mentioned in passing"}
        self.assertEqual(ticket_of(task), "TICKET-1")


class ATaskWithoutATicketGetsATitleTests(unittest.TestCase):
    def test_leading_filler_is_dropped(self):
        task = {"brief": "Please fix the silent mic hard stop before the release"}
        # "please" and "fix" say nothing about the subject; the name should.
        self.assertEqual(title_of(task), "silent-mic-hard")

    def test_filler_inside_a_phrase_is_kept(self):
        # Only LEADING noise is skipped -- "of" inside a phrase is part of it.
        task = {"brief": "Rollout of the watchdog"}
        self.assertEqual(title_of(task), "rollout-of-the")

    def test_a_leading_number_is_not_a_name(self):
        task = {"brief": "3 conflicts to resolve in the capture path"}
        self.assertTrue(title_of(task).startswith("conflicts"))

    def test_an_empty_brief_yields_no_title(self):
        self.assertEqual(title_of({"brief": ""}), "")
        self.assertEqual(title_of(None), "")


class ALineIsNeverNamelessTests(unittest.TestCase):
    def test_the_fallback_is_used_when_nothing_else_is_available(self):
        self.assertEqual(task_name({}, fallback="t-abc123"), "t-abc123")

    def test_the_id_is_the_last_resort(self):
        self.assertEqual(task_name({"id": "t-abc123"}), "t-abc123")

    def test_none_is_tolerated(self):
        # `pending` is the one command that must never fail, so every helper it
        # reaches has to survive a missing record.
        self.assertEqual(task_name(None, fallback="w-1"), "w-1")


class ARecordedTitleBeatsTheBriefTests(unittest.TestCase):
    """The case a driver's own brief cannot answer.

    Every other task can be named from its brief, because a brief opens by
    saying what the work is. A driver's brief is its role document, which
    opens by saying what a driver is -- so deriving from it named every driver
    in every project the same thing.
    """

    def test_the_recorded_title_is_used_over_the_brief(self):
        task = {
            "title": "silent mic hard stop",
            "brief": "You are this project's foreman. You own the loops inside",
        }
        self.assertEqual(task_name(task), "silent-mic-hard")

    def test_a_ticket_still_wins_over_a_recorded_title(self):
        task = {"ticket": "TICKET-123", "title": "silent mic hard stop"}
        self.assertEqual(task_name(task), "TICKET-123")

    def test_a_title_is_sanitized_not_trusted(self):
        # It is display-only and reaches report lines, so whatever comes in
        # leaves as lowercase words joined by dashes -- never as something a
        # terminal or a log line would read as structure.
        task = {"title": "ship\n\rit --now; rm -rf /"}
        self.assertEqual(task_name(task), "ship")

    def test_an_empty_title_falls_through_to_the_brief(self):
        task = {"title": "   ", "brief": "rebuild the export pipeline"}
        self.assertEqual(task_name(task), "rebuild-the-export")


class ADriversBriefIsNeverItsNameTests(unittest.TestCase):
    """The standing role document says what a driver IS, not what it is for.

    Slugged, it produced `project-s-foreman` -- identical for every driver in
    every project, and as a tab label it read "lead project-s-foreman", which
    is worse than the id it replaced because it looks meaningful.
    """

    ROLE_DOCUMENT = "You are this project's foreman. You own the loops inside one project"

    def test_a_driver_with_nothing_to_go_on_falls_back_to_the_id(self):
        task = {"id": "t-1", "role": "foreman", "brief": self.ROLE_DOCUMENT}
        self.assertEqual(task_name(task, fallback="w-abc"), "w-abc")

    def test_a_ticket_still_names_it(self):
        task = {"id": "t-1", "role": "foreman", "ticket": "TICKET-42", "brief": self.ROLE_DOCUMENT}
        self.assertEqual(task_name(task), "TICKET-42")

    def test_a_recorded_title_still_names_it(self):
        task = {"id": "t-1", "role": "foreman", "title": "silent mic hard stop",
                "brief": self.ROLE_DOCUMENT}
        self.assertEqual(task_name(task), "silent-mic-hard")

    def test_an_ordinary_task_is_still_named_from_its_brief(self):
        # A worker's brief IS the statement of its work, so nothing changes.
        task = {"id": "t-2", "role": "worker", "brief": "rebuild the export pipeline"}
        self.assertEqual(task_name(task), "rebuild-the-export")


class NameFromTextTests(unittest.TestCase):
    def test_leading_noise_is_dropped_but_interior_noise_is_kept(self):
        # "the" leads, so it is skipped; once a real word has landed the
        # same word is part of the phrase.
        self.assertEqual(name_from("the mux holds the slot"), "mux-holds-the")
        self.assertEqual(name_from("please fix the export pipeline"), "export-pipeline")

    def test_only_the_first_line_is_read(self):
        self.assertEqual(name_from("export pipeline\nand a lot of prose"), "export-pipeline")

    def test_nothing_usable_gives_an_empty_name(self):
        self.assertEqual(name_from("   "), "")
        self.assertEqual(name_from(None), "")


class NamesAreMadeUniqueWithoutPunishingTheCommonCaseTests(unittest.TestCase):
    def test_a_unique_name_keeps_its_bare_form(self):
        self.assertEqual(
            disambiguate({"t-1": "TICKET-1", "t-2": "TICKET-2"}),
            {"t-1": "TICKET-1", "t-2": "TICKET-2"},
        )

    def test_only_the_later_collisions_take_a_suffix(self):
        # The first keeps the bare name: one task per ticket is the norm and
        # must not grow a "-1" nobody needed.
        self.assertEqual(
            disambiguate({"t-1": "TICKET-1", "t-2": "TICKET-1", "t-3": "TICKET-1"}),
            {"t-1": "TICKET-1", "t-2": "TICKET-1-2", "t-3": "TICKET-1-3"},
        )

    def test_the_mapping_is_stable_for_a_stable_input(self):
        names = {"t-1": "a", "t-2": "a", "t-3": "b"}
        self.assertEqual(disambiguate(names), disambiguate(names))


if __name__ == "__main__":
    unittest.main()
