import unittest
from urllib.parse import urlsplit

from teams_transcription.meeting_paths import teams_join_path_allowed


class TeamsJoinPathTests(unittest.TestCase):
    def test_encoded_standard_id_optional_zero_and_query(self):
        ident = '19%3ameeting_synthetic-id%40thread.v2'
        for url in (
            f'https://teams.microsoft.com/l/meetup-join/{ident}',
            f'https://teams.microsoft.com/l/meetup-join/{ident}/0?context=%7B%22Tid%22%3A%22synthetic%22%7D',
            'https://teams.cloud.microsoft/meet/123456?context=synthetic',
        ):
            self.assertTrue(teams_join_path_allowed(urlsplit(url).path), url)

    def test_extra_segments_empty_ids_and_path_injections_rejected(self):
        for path in ('/l/meetup-join/', '/l/meetup-join//0', '/l/meetup-join/id/1',
                     '/l/meetup-join/id/0/extra', '/l/meetup-join/id/0/',
                     '/l/meetup-join/id/extra/0', '/meet/', '/meet/id/0',
                     '/meet/id/extra', '//l/meetup-join/id/0',
                     '/dl/launcher/id', '/v2/id', '/l/meetup-join/id/../0'):
            self.assertFalse(teams_join_path_allowed(path), path)
