import csv
import io
import unittest
from unittest import mock

import pandas as pd  # type: ignore
from tests.common import AppMixin
from tests.common import debug_on

from flask_app import db
from flask_app import utils
from flask_app.app import API_URL_PREFIX
from flask_app.modeling.train_queue import Scheduler
from flask_app.utils import Json


class TestTopicModels(AppMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        # Create a topic model in the database
        with self._app.app_context():
            self._num_topics = 10
            self._topic_mdl = db.TopicModel.create(
                name="test_topic_model", num_topics=self._num_topics
            )

            # Make sure the directory for the topic model exists
            utils.Files.topic_model_dir(self._topic_mdl.id_, ensure_exists=True)

            self._valid_training_table = [
                [cell]
                for cell in [
                    # Taken from huffpost.com
                    f"{utils.CONTENT_COL}",
                    "Florida Officer Who Was Filmed Shoving A Kneeling Black Protester Has Been Charged",
                    "Fox News Host Ed Henry Fired After Sexual Misconduct Investigation",
                    "Hong Kong Police Make First Arrests Under New Security Law Imposed By China",
                    "As Democrats Unveil Ambitious Climate Goals, House Lawmakers Press For Green Stimulus",
                    "Citing Racial Bias, San Francisco Will End Mug Shots Release",
                    "‘Your Chewing Sounds Like Nails On A Chalkboard’: What Life With Misophonia Is Like",
                    "Puerto Rico’s Troubled Utility Is A Goldmine For U.S. Contractors",
                    "Schools Provide Stability For Refugees. COVID-19 Upended That.",
                    "Jada Pinkett Smith Denies Claim Will Smith Gave Blessing To Alleged Affair",
                    "College Students Test Positive For Coronavirus, Continue Going To Parties Anyway",
                    "A TikTok User Noticed A Creepy Thing In ‘Glee’ You Can’t Unsee",
                    "Prince Harry Speaks Out Against Institutional Racism: It Has ‘No Place’ In Society",
                    "A Poet — Yes, A Poet — Makes History On ‘America’s Got Talent’",
                    "I Ate At A Restaurant In What Was Once COVID-19’s Deadliest County",
                    "This Is What Racial Trauma Does To The Body And Brain",
                    "How To Avoid Bad Credit As Protections In The CARES Act Expire",
                    "Here’s Proof We Need Better Mental Health Care For People Of Color",
                    "“I hope that this is real,” Lauren Boebert said of the deep-state conspiracy theory.",
                    "U.S. Buys Virtually All Of Coronavirus Drug Remdesivir In The World",
                    "Trials found that the anti-viral drug can reduce the recovery time of COVID-19 patients by four days.",
                    "Florida Gov. Ron DeSantis Says He Won’t Reinstate Restrictions Despite COVID-19 Surge",
                    "Conservative Columnist Spells Out Exactly Who’s To Blame For U.S. Coronavirus Failings",
                    "'We are living — and now dying — in an idiocracy of our own creation,' said The Washington Post's Max Boot.",
                    "Lori Vallow Daybell allegedly conspired with her new husband to hide or destroy the bodies on his rural Idaho property.",
                    "Obama Photographer Flags Yet Another Wild Difference With The Trump Presidency",
                    "Viola Davis’ Call To ‘Pay Me What I’m Worth’ Is What The World Needs Now",
                    "The Oscar winner's fierce declaration reemerged on social media to turbocharge calls for equity in Hollywood.",
                    "Anderson Cooper Breaks Down Why Trump’s America Is Now ‘A Pariah State’",
                ]
            ]

        # What we expect to see after upload file being processed in the backend
        self._expected_training_table = [[utils.ID_COL, utils.CONTENT_COL]] + [
            [str(row_num), cell]
            for row_num, (cell,) in enumerate(self._valid_training_table[1:], start=1)
        ]

    def test_list(self) -> None:
        url = API_URL_PREFIX + "/topic_models/"
        expected_topic_model_json = {
            "topic_model_id": self._topic_mdl.id_,
            "topic_model_name": "test_topic_model",
            "num_topics": self._num_topics,
            "topic_names": None,
            "status": "not_begun",
        }
        with self._app.test_client() as client, self._app.app_context():
            resp = client.get(url)
            self._assert_response_success(resp, url)
            resp_json = resp.get_json()
            assert isinstance(resp_json, list)
            expected_topic_model_list_json = [expected_topic_model_json]
            self.assertListEqual(resp_json, expected_topic_model_list_json)

    def test_create(self) -> None:
        url = API_URL_PREFIX + "/topic_models/"

        with self._app.test_client() as client, self._app.app_context():
            with self.subTest("creating a topic model"):
                resp = client.post(
                    url, json={"topic_model_name": "test_topic_model", "num_topics": 2}
                )
                self._assert_response_success(resp, url)
                resp_json: Json = resp.get_json()

                assert isinstance(resp_json, dict)
                expected_topic_model_json = {
                    "topic_model_name": "test_topic_model",
                    "num_topics": 2,
                    "topic_names": None,
                    "status": "not_begun",
                }

                resp_json.pop("topic_model_id")
                self.assertDictEqual(
                    resp_json, expected_topic_model_json,
                )

    @debug_on()
    def test_trigger_training(self) -> None:
        # Mock the scheduler
        with self._app.app_context():
            scheduler: Scheduler = self._app.config["SCHEDULER"]
            scheduler.add_topic_model_training: mock.MagicMock = mock.MagicMock(return_value=None)  # type: ignore
            fname_keywords = utils.Files.topic_model_keywords_file(self._topic_mdl.id_)
            fname_topics_by_doc = utils.Files.topic_model_probabilities_by_example_file(
                self._topic_mdl.id_
            )
            training_file_path = utils.Files.topic_model_training_file(
                self._topic_mdl.id_
            )

        test_url = API_URL_PREFIX + f"/topic_models/{self._topic_mdl.id_}/training/file"
        # Prepare the file to "upload"
        text_io = io.StringIO()
        writer = csv.writer(text_io)
        writer.writerows(self._valid_training_table)
        text_io.seek(0)
        to_upload_file = io.BytesIO(text_io.read().encode())

        with self._app.test_client() as client, self._app.app_context():
            res = client.post(test_url, data={"file": (to_upload_file, "train.csv")},)
            self._assert_response_success(res)

        # Assert that the correct training file was created in the correct directory
        self.assertTrue(training_file_path.exists())

        # Assert that the content of the training file matches
        self.maxDiff = 10000
        with training_file_path.open() as created_train_file:
            reader = csv.reader(created_train_file)
            created_training_table = list(reader)
        # The created training file should  have an ID column prepended
        self.assertSequenceEqual(created_training_table, self._expected_training_table)

        # Asssert the scheduler was called with the right arguments
        scheduler.add_topic_model_training.assert_called_with(
            training_file=str(training_file_path),
            num_topics=self._num_topics,
            fname_keywords=str(fname_keywords),
            fname_topics_by_doc=str(fname_topics_by_doc),
        )

    @debug_on()
    def test_actual_training(self) -> None:

        with self._app.app_context():
            # Get some variables
            scheduler: Scheduler = self._app.config["SCHEDULER"]
            training_file_path = utils.Files.topic_model_training_file(
                self._topic_mdl.id_
            )
            fname_keywords = utils.Files.topic_model_keywords_file(self._topic_mdl.id_)
            fname_topics_by_doc = utils.Files.topic_model_probabilities_by_example_file(
                self._topic_mdl.id_
            )

            # Update db
            lda_set = db.LDASet()
            lda_set.save()
            self._topic_mdl.lda_set = lda_set
            self._topic_mdl.save()

        # Create the training file
        with training_file_path.open("w") as f:
            writer = csv.writer(f)
            writer.writerows(self._expected_training_table)

        # Start the training
        scheduler.add_topic_model_training(
            training_file=str(training_file_path),
            num_topics=self._num_topics,
            fname_keywords=str(fname_keywords),
            fname_topics_by_doc=str(fname_topics_by_doc),
            iterations=10,
        )

        self.assertTrue(fname_keywords.exists())
        self.assertTrue(fname_topics_by_doc.exists())

        # Inspect the content of the keywords file
        fname_keywords_df = pd.read_excel(fname_keywords, index_col=0, header=0)
        expected_fname_keywords_index = pd.Index(
            [f"word_{i}" for i in range(utils.DEFAULT_NUM_KEYWORDS_TO_GENERATE)]
            + ["proportions"]
        )
        pd.testing.assert_index_equal(
            fname_keywords_df.index, expected_fname_keywords_index
        )
        expected_fname_keywords_columns = pd.Index(range(self._num_topics))
        pd.testing.assert_index_equal(
            fname_keywords_df.columns, expected_fname_keywords_columns
        )

        # Inspect the fname_topics_by_doc file
        fname_topics_by_doc_df = pd.read_excel(
            fname_topics_by_doc, index_col=0, header=0
        )
        num_examples = len(self._valid_training_table) - 1  # -1 for the header
        expected_fname_topics_by_doc_index = pd.Index(range(num_examples))
        pd.testing.assert_index_equal(
            fname_topics_by_doc_df.index, expected_fname_topics_by_doc_index
        )
        expected_fname_topics_by_doc_columns = pd.Index(
            [utils.ID_COL, utils.CONTENT_COL]
            + [f"proba_topic_{i}" for i in range(self._num_topics)]
            + ["most_likely_topic"]
        )
        pd.testing.assert_index_equal(
            fname_topics_by_doc_df.columns, expected_fname_topics_by_doc_columns,
        )

    def naming_topic(self) -> None:
        topic_mdl = db.TopicModel.create(name="test_topic_model", num_topics=10)
        utils.Files.topic_model_dir(topic_mdl.id_, ensure_exists=True)
        _ = API_URL_PREFIX + f"/topic_models/{topic_mdl.id_}/training/file"
        pass


if __name__ == "__main__":
    unittest.main()