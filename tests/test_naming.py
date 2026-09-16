import unittest

from helm.naming import disambiguate, task_name, ticket_of, title_of


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

    def test_only_the_first_line_is_read_for_a_ticket(self):
        # Later prose mentions other work; matching it would name a task after
        # whatever it happened to reference.
        task = {"brief": "Resolve the merge conflicts\n\nRelated: OTHER-999"}
        self.assertNotEqual(task_name(task), "OTHER-999")

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
