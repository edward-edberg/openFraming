"""All the flask api endpoints."""
import csv
import functools
import logging
import re
import typing as T
from collections import Counter
from pathlib import Path

import pandas as pd  # type: ignore
import peewee as pw
import typing_extensions as TT
from flask import current_app
from flask import Flask
from flask import has_app_context
from flask import Response
from flask import send_file
from flask_restful import Api  # type: ignore
from flask_restful import reqparse
from flask_restful import Resource
from playhouse.flask_utils import get_object_or_404
from sklearn import model_selection  # type: ignore
from typing_extensions import TypedDict
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import BadRequest
from werkzeug.exceptions import HTTPException
from werkzeug.exceptions import NotFound

from flask_app import utils
from flask_app.database import commands as database_commands
from flask_app.database import models
from flask_app.modeling.classifier import ClassifierMetricsJson
from flask_app.modeling.lda import TopicModelMetricsJson
from flask_app.modeling.queue_manager import QueueManager
from flask_app.settings import needs_settings_init
from flask_app.settings import Settings
from flask_app.version import Version

API_URL_PREFIX = "/api"

logger = logging.getLogger(__name__)


class HasReqParseProtocol(TT.Protocol):
    reqparse: reqparse.RequestParser


class SupportSpreadsheetFileType(object):
    def __init__(self: HasReqParseProtocol) -> None:
        super().__init__()  # type: ignore[misc]
        choices = [
            file_type.strip(".")
            for file_type in Settings.SUPPORTED_NON_CSV_FORMATS | {".csv"}
        ]
        self.reqparse.add_argument(
            "file_type", type=str, choices=choices, location="args", default="xlsx"
        )

    def _get_cached_version_with_file_type(
        self, file_path: Path, file_type: TT.Literal[".xlsx", ".xls", ".csv"]
    ) -> Path:
        assert file_type[0] == ".", "file type needs to have a dot in the beginning."
        assert (
            file_path.suffix == ".csv"
        ), "We're only using CSV as the internal file format."

        if file_type == file_path.suffix:
            return file_path
        elif file_type in Settings.SUPPORTED_NON_CSV_FORMATS:
            file_path_with_type = file_path.parent / (file_path.stem + file_type)
            with file_path.open() as f:
                df = pd.read_csv(
                    f, dtype=object, header=None, index_col=False, na_filter=False
                )

            excel_writer = pd.ExcelWriter(file_path_with_type)
            df.to_excel(excel_writer, index=False, header=False)
            excel_writer.save()
            return file_path_with_type
        else:
            raise RuntimeError("Unknown/malformed file type passed: " + file_type)


class UnprocessableEntity(HTTPException):
    """."""

    code = 422
    description = "The entity supplied has errors and cannot be processed."


class AlreadyExists(HTTPException):
    """."""

    code = 403
    description = "The resource already exists."


email_expr = re.compile(r"\"?([-a-zA-Z0-9.`?{}]+@\w+\.\w+)\"?")


class ResourceProtocol(TT.Protocol):

    url: str  # Our own addition


class BaseResource(Resource):
    """Every resource derives from this.

    Attributes:
        url:
    """

    url: str

    @staticmethod
    def _write_headers_and_data_to_csv(
        headers: T.List[str], data: T.List[T.List[str]], csvfile: Path
    ) -> None:

        with csvfile.open("w") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(data)

    @staticmethod
    def _validate_serializable_list_value(val: T.Any) -> str:
        if not isinstance(val, str):
            raise ValueError("must be str")
        if "," in val:
            raise ValueError("can't contain commas.")
        return val

    @staticmethod
    def _validate_email(val: T.Any) -> str:
        if not isinstance(val, str):
            raise ValueError("must be str")
        elif re.match(email_expr, val):
            return val
        else:
            raise ValueError("Not a valid email.")


class ClassifierStatusJson(TypedDict):
    classifier_id: int
    classifier_name: str
    category_names: T.List[str]
    trained_by_openFraming: bool
    status: TT.Literal["not_begun", "training", "error_encountered", "completed"]
    notify_at_email: str
    metrics: T.Optional[ClassifierMetricsJson]


class ClassifierRelatedResource(BaseResource):
    """Base class to define utility functions related to classifiers."""

    @staticmethod
    def _classifier_status(clsf: models.Classifier) -> ClassifierStatusJson:
        """Process a Classifier instance and format it into the API spec."""
        metrics: T.Optional[ClassifierMetricsJson] = None
        status: TT.Literal[
            "not_begun", "error_encountered", "completed", "training"
        ] = "not_begun"
        if clsf.train_set is not None:
            assert clsf.dev_set is not None
            if clsf.train_set.training_or_inference_completed:
                assert clsf.dev_set.training_or_inference_completed
                assert clsf.dev_set.metrics is not None
                status = "completed"
                metrics = ClassifierMetricsJson(
                    accuracy=clsf.dev_set.metrics.accuracy,
                    macro_f1_score=clsf.dev_set.metrics.macro_f1_score,
                    macro_precision=clsf.dev_set.metrics.macro_precision,
                    macro_recall=clsf.dev_set.metrics.macro_recall,
                )
            elif clsf.train_set.error_encountered:
                assert clsf.dev_set.error_encountered
                status = "error_encountered"
            else:
                status = "training"

        category_names = clsf.category_names

        return ClassifierStatusJson(
            {
                "classifier_id": clsf.classifier_id,
                "classifier_name": clsf.name,
                "trained_by_openFraming": clsf.trained_by_openFraming,
                "category_names": category_names,
                "notify_at_email": clsf.notify_at_email,
                "status": status,
                "metrics": metrics,
            }
        )


class OneClassifier(ClassifierRelatedResource):

    url = "/classifiers/<int:classifier_id>"

    def get(self, classifier_id: int) -> ClassifierStatusJson:
        clsf = get_object_or_404(
            models.Classifier, models.Classifier.classifier_id == classifier_id
        )
        return self._classifier_status(clsf)


class Classifiers(ClassifierRelatedResource):
    """Create a classifer, get a list of classifiers."""

    url = "/classifiers/"

    def __init__(self) -> None:
        """Set up request parser."""
        self.reqparse = reqparse.RequestParser()
        self.reqparse.add_argument(
            name="name", type=str, required=True, location="json"
        )
        self.reqparse.add_argument(
            name="notify_at_email",
            type=self._validate_email,
            required=True,
            location="json",
            help="The email address provided must be a valid email address.",
        )
        self.reqparse.add_argument(
            name="category_names",
            type=self._validate_serializable_list_value,
            action="append",
            required=True,
            location="json",
            help="The category names must be a list of strings that don't contain commas within them..",
        )

    def post(self) -> ClassifierStatusJson:
        """Create a classifier."""

        args = self.reqparse.parse_args()
        category_names = args["category_names"]
        utils.Validate.no_duplicates(category_names)
        utils.Validate.not_just_one(category_names)
        name = args["name"]
        notify_at_email = args["notify_at_email"]
        clsf = models.Classifier.create(
            name=name, category_names=category_names, notify_at_email=notify_at_email
        )
        clsf.save()
        utils.Files.classifier_dir(classifier_id=clsf.classifier_id, ensure_exists=True)
        return self._classifier_status(clsf)

    def get(self) -> T.List[ClassifierStatusJson]:
        """Get a list of classifiers."""
        res = [self._classifier_status(clsf) for clsf in models.Classifier.select()]
        return res


class ClassifiersTrainingFile(ClassifierRelatedResource):
    """Upload training data to the classifier."""

    url = "/classifiers/<int:classifier_id>/training/file"

    def __init__(self) -> None:
        """Set up request parser."""
        self.reqparse = reqparse.RequestParser()
        self.reqparse.add_argument(
            name="file", type=FileStorage, required=True, location="files"
        )

    def post(self, classifier_id: int) -> ClassifierStatusJson:
        """Upload a training set for classifier, and start training.

        Body:
            FormData: with "file" item. 

        Raises:
            BadRequest
            UnprocessableEntity

        """
        args = self.reqparse.parse_args()
        file_: FileStorage = args["file"]

        try:
            classifier = models.Classifier.get(
                models.Classifier.classifier_id == classifier_id
            )
        except models.Classifier.DoesNotExist:
            raise NotFound("classifier not found.")

        if classifier.train_set is not None:
            raise AlreadyExists("This classifier already has a training set.")

        table_headers, table_data = self._validate_training_file_and_get_data(
            classifier.category_names, file_
        )
        file_.close()
        # Split into train and dev
        ss = model_selection.StratifiedShuffleSplit(n_splits=1, test_size=0.2)
        X, y = zip(*table_data)
        train_indices, dev_indices = next(ss.split(X, y))

        train_data = [table_data[i] for i in train_indices]
        dev_data = [table_data[i] for i in dev_indices]

        train_file = utils.Files.classifier_train_set_file(classifier_id)
        self._write_headers_and_data_to_csv(table_headers, train_data, train_file)
        dev_file = utils.Files.classifier_dev_set_file(classifier_id)
        self._write_headers_and_data_to_csv(table_headers, dev_data, dev_file)

        classifier.train_set = models.LabeledSet()
        classifier.dev_set = models.LabeledSet()
        classifier.train_set.save()
        classifier.dev_set.save()
        classifier.save()

        # Refresh classifier
        classifier = models.Classifier.get(
            models.Classifier.classifier_id == classifier_id
        )

        queue_manager: QueueManager = current_app.queue_manager

        # TODO: Add a check to make sure model training didn't start already and crashed

        queue_manager.add_classifier_training(
            classifier_id=classifier.classifier_id,
            labels=classifier.category_names,
            model_path=Settings.TRANSFORMERS_MODEL,
            train_file=str(utils.Files.classifier_train_set_file(classifier_id)),
            dev_file=str(utils.Files.classifier_dev_set_file(classifier_id)),
            cache_dir=str(Settings.TRANSFORMERS_CACHE_DIRECTORY),
            output_dir=str(
                utils.Files.classifier_output_dir(classifier_id, ensure_exists=True)
            ),
        )

        return self._classifier_status(classifier)

    @staticmethod
    def _validate_training_file_and_get_data(
        category_names: T.List[str], file_: FileStorage
    ) -> T.Tuple[T.List[str], T.List[T.List[str]]]:
        """Validate user uploaded file and return uploaded validated data.

        Args:
            category_names: The categories for the classifier.
            file_: uploaded file.

        Returns:
            table_headers: A list of length 2.
            table_data: A list of lists of length 2.
        """
        # TODO: Write tests for all of these!

        table = utils.Validate.spreadsheet_and_get_table(file_)

        utils.Validate.table_has_no_empty_cells(table)
        utils.Validate.table_has_num_columns(table, 2)
        utils.Validate.table_has_headers(
            table, [Settings.CONTENT_COL, Settings.LABEL_COL]
        )

        table_headers, table_data = table[0], table[1:]

        min_num_examples = int(len(table_data) * Settings.TEST_SET_SPLIT)
        if len(table_data) < min_num_examples:
            raise BadRequest(
                f"We need at least {min_num_examples} labelled examples for this issue."
            )

        # TODO: Low priority: Make this more efficient.
        category_names_counter = Counter(category for _, category in table_data)

        unique_category_names = category_names_counter.keys()
        if set(category_names) != unique_category_names:
            # TODO: Lower case category names before checking.
            # TODO: More helpful error messages when there is an error with the
            # the categories in an uploaded training file.
            raise UnprocessableEntity(
                "The categories for this classifier are"
                f" {category_names}. But the uploaded file either"
                " has some categories missing, or has categories in addition to the"
                " ones indicated."
            )

        categories_with_less_than_two_exs = [
            category for category, count in category_names_counter.items() if count < 2
        ]
        if categories_with_less_than_two_exs:
            raise UnprocessableEntity(
                "There are less than two examples with the categories: "
                f"{','.join(categories_with_less_than_two_exs)}."
                " We need at least two examples per category."
            )

        return table_headers, table_data


class ClassifierTestSetStatusJson(TypedDict):
    classifier_id: int
    test_set_id: int
    test_set_name: str
    notify_at_email: str
    status: TT.Literal["not_begun", "predicting", "error_encountered", "completed"]


class ClassifierTestSetRelatedResource(ClassifierRelatedResource):
    @staticmethod
    def _test_set_status(test_set: models.TestSet) -> ClassifierTestSetStatusJson:
        status: TT.Literal[
            "not_begun", "predicting", "error_encountered", "completed"
        ] = "not_begun"
        if test_set.inference_began:
            if test_set.inference_completed:
                status = "completed"
            elif test_set.error_encountered:
                status = "error_encountered"
            else:
                status = "predicting"

        return ClassifierTestSetStatusJson(
            classifier_id=test_set.classifier.classifier_id,
            test_set_id=test_set.id_,
            test_set_name=test_set.name,
            notify_at_email=test_set.notify_at_email,
            status=status,
        )


class OneClassifierTestSet(ClassifierTestSetRelatedResource):

    url = "/classifiers/<int:classifier_id>/test_sets/<int:test_set_id>"

    def get(self, classifier_id: int, test_set_id: int) -> ClassifierTestSetStatusJson:
        test_set = get_object_or_404(models.TestSet, models.TestSet.id_ == test_set_id)
        if test_set.classifier.classifier_id != classifier_id:
            raise NotFound("!test set not found.")
        return self._test_set_status(test_set)


class ClassifiersTestSets(ClassifierTestSetRelatedResource):
    """Upload training data to the classifier."""

    url = "/classifiers/<int:classifier_id>/test_sets/"

    def __init__(self) -> None:
        """Set up request parser."""
        self.reqparse = reqparse.RequestParser()
        self.reqparse.add_argument(
            name="test_set_name", type=str, required=True, location="json"
        )
        self.reqparse.add_argument(
            name="notify_at_email",
            type=self._validate_email,
            required=True,
            location="json",
        )

    def get(self, classifier_id: int) -> T.List[ClassifierTestSetStatusJson]:
        clsf = get_object_or_404(
            models.Classifier, models.Classifier.classifier_id == classifier_id
        )

        return [self._test_set_status(test_set) for test_set in clsf.test_sets]

    def post(self, classifier_id: int) -> ClassifierTestSetStatusJson:
        args = self.reqparse.parse_args()
        test_set_name: str = args["test_set_name"]
        notify_at_email: str = args["notify_at_email"]

        try:
            classifier = models.Classifier.get(
                models.Classifier.classifier_id == classifier_id
            )
        except models.Classifier.DoesNotExist:
            raise NotFound("classifier not found.")

        if classifier.train_set is None:
            assert classifier.dev_set is None
            raise BadRequest("This classifier has not been trained yet.")
        elif not classifier.train_set.training_or_inference_completed:
            assert classifier.dev_set is not None
            assert not classifier.dev_set.training_or_inference_completed
            raise BadRequest("This classifier's training has not been completed yet.")

        test_set = models.TestSet.create(
            classifier=classifier, name=test_set_name, notify_at_email=notify_at_email
        )

        # Create directory for test set
        utils.Files.classifier_test_set_dir(
            classifier_id, test_set.id_, ensure_exists=True
        )

        return self._test_set_status(test_set)


class ClassifiersTestSetsPredictions(
    ClassifierTestSetRelatedResource, SupportSpreadsheetFileType
):
    url = "/classifiers/<int:classifier_id>/test_sets/<int:test_set_id>/predictions"

    def __init__(self) -> None:
        self.reqparse = reqparse.RequestParser()
        SupportSpreadsheetFileType.__init__(self)

    def get(self, classifier_id: int, test_set_id: int) -> Response:
        test_set = get_object_or_404(models.TestSet, models.TestSet.id_ == test_set_id)
        if test_set.classifier.classifier_id != classifier_id:
            raise NotFound(
                "Please check the classifier id and the test set id. They don't match."
            )
        if not test_set.inference_began:
            raise BadRequest(
                "No data has been uploaded to the test set yet. Please upload test data first."
            )
        if not test_set.inference_completed:
            raise BadRequest("Inference on this test set has not been completed yet.")
        else:
            test_file = utils.Files.classifier_test_set_predictions_file(
                classifier_id, test_set_id
            )

            args = self.reqparse.parse_args()
            file_type_with_dot = "." + args["file_type"]

            test_file_with_file_type = self._get_cached_version_with_file_type(
                test_file, file_type=file_type_with_dot
            )
            name_for_file = f"Classifier_{test_set.classifier.name}-test_set_{test_set.name}{file_type_with_dot}"
            return send_file(
                test_file_with_file_type,
                as_attachment=True,
                attachment_filename=name_for_file,
            )


class ClassifiersTestSetsFile(ClassifierTestSetRelatedResource):
    """Upload training data to the classifier."""

    url = "/classifiers/<int:classifier_id>/test_sets/<int:test_set_id>/file"

    def __init__(self) -> None:
        """Set up request parser."""
        self.reqparse = reqparse.RequestParser()
        self.reqparse.add_argument(
            name="file", type=FileStorage, required=True, location="files"
        )

    def post(self, classifier_id: int, test_set_id: int) -> ClassifierTestSetStatusJson:
        """Upload a training set for classifier, and start training.

        Body:
            FormData: with "file" item. 

        Raises:
            BadRequest
            UnprocessableEntity
            NotFound
        """
        args = self.reqparse.parse_args()
        file_: FileStorage = args["file"]

        test_set = get_object_or_404(models.TestSet, models.TestSet.id_ == test_set_id)
        if test_set.classifier.classifier_id != classifier_id:
            raise NotFound(
                "Please check the classifier id and test set id. They don't match."
            )

        if test_set.inference_began:
            raise AlreadyExists("The file for this test set has already been uploaded.")

        table_headers, table_data = self._validate_test_file_and_get_data(file_)

        test_file = utils.Files.classifier_test_set_file(classifier_id, test_set_id)
        self._write_headers_and_data_to_csv(table_headers, table_data, test_file)

        test_set.inference_began = True
        test_set.save()

        queue_manager: QueueManager = current_app.queue_manager

        # TODO: Add a check to make sure model training didn't start already and crashed

        test_output_file = utils.Files.classifier_test_set_predictions_file(
            classifier_id, test_set_id
        )
        model_path = utils.Files.classifier_output_dir(classifier_id)

        queue_manager.add_classifier_prediction(
            test_set_id=test_set_id,
            labels=test_set.classifier.category_names,
            model_path=str(model_path),
            test_file=str(test_file),
            cache_dir=str(Settings.TRANSFORMERS_CACHE_DIRECTORY),
            test_output_file=str(test_output_file),
        )

        return self._test_set_status(test_set)

    @staticmethod
    def _validate_test_file_and_get_data(
        file_: FileStorage,
    ) -> T.Tuple[T.List[str], T.List[T.List[str]]]:
        """Validate user uploaded file and return validated data.

        Args:
            file_: uploaded file.
            category_names: The categories for the classifier.

        Returns:
            table_headers: A list of length 2.
            table_data: A list of lists of length 2.
        """

        table = utils.Validate.spreadsheet_and_get_table(file_)

        utils.Validate.table_has_no_empty_cells(table)
        utils.Validate.table_has_num_columns(table, 1)
        utils.Validate.table_has_headers(table, [Settings.CONTENT_COL])
        table_headers, table_data = table[0], table[1:]

        min_num_examples = 1
        if len(table_data) < min_num_examples:
            raise BadRequest(
                f"We need at least {min_num_examples} examples to run prediction on."
            )

        return table_headers, table_data


class TopicModelStatusJson(TypedDict):
    topic_model_id: int
    topic_model_name: str
    num_topics: int
    topic_names: T.Optional[T.List[str]]
    notify_at_email: str
    metrics: T.Optional[TopicModelMetricsJson]
    status: TT.Literal[
        "not_begun", "training", "topics_to_be_named", "error_encountered", "completed"
    ]
    # TODO: Update backend README to reflect API change for line above.


class OneTopicPreviewJson(TypedDict):
    keywords: T.List[str]
    examples: T.List[str]


class TopicModelPreviewJson(TopicModelStatusJson):
    topic_previews: T.List[OneTopicPreviewJson]


class TopicModelRelatedResource(BaseResource):
    """Base class to define utility functions related to classifiers."""

    @staticmethod
    def _ensure_topic_names(topic_mdl: models.TopicModel) -> models.TopicModel:
        # TODO: Remove this. This is only for compatibility reasons.
        # We didn't assign default topic names before.
        if topic_mdl.topic_names is None:
            topic_mdl.topic_names = [
                Settings.DEFAULT_TOPIC_NAME_TEMPLATE.format(topic_num)
                for topic_num in range(1, topic_mdl.num_topics + 1)
            ]
            topic_mdl.save()
        return topic_mdl.refresh()

    @staticmethod
    def _topic_model_status_json(topic_mdl: models.TopicModel) -> TopicModelStatusJson:
        topic_names = topic_mdl.topic_names
        status: TT.Literal[
            "not_begun",
            "training",
            "error_encountered",
            "topics_to_be_named",
            "completed",
        ]
        metrics: T.Optional[TopicModelMetricsJson] = None
        if topic_mdl.lda_set is None:
            status = "not_begun"
        else:
            if topic_mdl.lda_set.lda_completed:
                if topic_names is None:
                    status = "topics_to_be_named"
                else:
                    status = "completed"
                status = "completed"
                # TODO: This check is necesary because metrics were added later/some topic
                # models don't have metrics. In the future, this should be removed.
                if topic_mdl.lda_set.metrics is not None:
                    metrics = TopicModelMetricsJson(
                        umass_coherence=topic_mdl.lda_set.metrics.umass_coherence
                    )

            elif topic_mdl.lda_set.error_encountered:
                status = "error_encountered"
            else:
                status = "training"

        return TopicModelStatusJson(
            topic_model_name=topic_mdl.name,
            topic_model_id=topic_mdl.id_,
            num_topics=topic_mdl.num_topics,
            topic_names=topic_names,
            notify_at_email=topic_mdl.notify_at_email,
            status=status,
            metrics=metrics,
        )

    @staticmethod
    def _validate_topic_model_finished_training(topic_mdl: models.TopicModel) -> None:
        if topic_mdl.lda_set is None:
            raise BadRequest("Topic model has not started training yet.")
        elif not topic_mdl.lda_set.lda_completed:
            raise BadRequest("Topic model has not finished trianing yet.")


class OneTopicModel(TopicModelRelatedResource):

    url = "/topic_models/<int:topic_model_id>"

    def get(self, topic_model_id: int) -> TopicModelStatusJson:
        topic_mdl = get_object_or_404(
            models.TopicModel, models.TopicModel.id_ == topic_model_id
        )
        return self._topic_model_status_json(topic_mdl)


class TopicModels(TopicModelRelatedResource):

    url = "/topic_models/"

    def __init__(self) -> None:
        """Set up request parser."""
        self.reqparse = reqparse.RequestParser()
        self.reqparse.add_argument(
            name="topic_model_name", type=str, required=True, location="json"
        )

        def greater_than_1(x: T.Any) -> int:
            int_x = int(x)
            if int_x <= 1:
                raise ValueError("Must be greater than 1")
            return int_x

        self.reqparse.add_argument(
            name="num_topics",
            type=greater_than_1,
            required=True,
            location="json",
            help="The number of topics must be an integer greater than 1.",
        )
        self.reqparse.add_argument(
            name="notify_at_email",
            type=self._validate_email,
            required=True,
            location="json",
        )

    def post(self) -> TopicModelStatusJson:
        """Create a classifier."""
        args = self.reqparse.parse_args()
        topic_mdl = models.TopicModel.create(
            name=args["topic_model_name"],
            topic_names=[f"Topic {i}" for i in range(1, args["num_topics"] + 1)],
            num_topics=args["num_topics"],
            notify_at_email=args["notify_at_email"],
        )
        # Default topic names
        topic_mdl.save()
        utils.Files.topic_model_dir(id_=topic_mdl.id_, ensure_exists=True)
        return self._topic_model_status_json(topic_mdl)

    def get(self) -> T.List[TopicModelStatusJson]:
        res = [
            self._topic_model_status_json(topic_mdl)
            for topic_mdl in models.TopicModel.select()
        ]
        return res


class TopicModelsTrainingFile(TopicModelRelatedResource):

    url = "/topic_models/<int:id_>/training/file"

    def __init__(self) -> None:
        """Set up request parser."""
        self.reqparse = reqparse.RequestParser()
        self.reqparse.add_argument(
            name="file", type=FileStorage, required=True, location="files"
        )

    def post(self, id_: int) -> TopicModelStatusJson:
        args = self.reqparse.parse_args()
        file_: FileStorage = args["file"]

        try:
            topic_mdl = models.TopicModel.get(models.TopicModel.id_ == id_)
        except models.TopicModel.DoesNotExist:
            raise NotFound("The topic model was not found.")

        if topic_mdl.lda_set is not None:
            raise AlreadyExists("This topic model already has a training set.")

        table_headers, table_data = self._validate_and_get_training_file(file_)
        file_.close()

        train_file = utils.Files.topic_model_training_file(id_)
        self._write_headers_and_data_to_csv(table_headers, table_data, train_file)

        queue_manager: QueueManager = current_app.queue_manager

        topic_mdl = self._ensure_topic_names(topic_mdl)
        queue_manager.add_topic_model_training(
            topic_model_id=topic_mdl.id_,
            training_file=str(train_file),
            fname_keywords=str(utils.Files.topic_model_keywords_file(id_)),
            fname_topics_by_doc=str(utils.Files.topic_model_topics_by_doc_file(id_)),
            mallet_bin_directory=str(Settings.MALLET_BIN_DIRECTORY),
        )
        topic_mdl.lda_set = models.LDASet()
        topic_mdl.lda_set.save()
        topic_mdl.save()

        # Refresh classifier
        topic_mdl = models.TopicModel.get(models.TopicModel.id_ == id_)

        return self._topic_model_status_json(topic_mdl)

    @staticmethod
    def _validate_and_get_training_file(
        file_: FileStorage,
    ) -> T.Tuple[T.List[str], T.List[T.List[str]]]:
        """Validate user input and return uploaded CSV data.

        Args:
            file_: uploaded file.

        Returns:
            table_headers: A list of length 2.
            table_data: A list of lists of length 2.
        """
        # TODO: Write tests for all of these!

        table = utils.Validate.spreadsheet_and_get_table(file_)

        utils.Validate.table_has_num_columns(table, 1)
        utils.Validate.table_has_headers(table, [Settings.CONTENT_COL])
        utils.Validate.table_has_no_empty_cells(table)

        table_headers, table_data = table[0], table[1:]
        # Add the ID column to the table
        table_headers = [Settings.ID_COL] + table_headers
        table_data = [[str(row_num)] + row for row_num, row in enumerate(table_data)]

        if len(table_data) < Settings.MINIMUM_LDA_EXAMPLES:
            raise BadRequest(
                f"We need at least {Settings.MINIMUM_LDA_EXAMPLES} for a topic model."
            )

        return table_headers, table_data


class TopicModelsTopicsNames(TopicModelRelatedResource):

    url = "/topic_models/<int:id_>/topics/names"

    def __init__(self) -> None:
        self.reqparse = reqparse.RequestParser()

        self.reqparse.add_argument(
            name="topic_names",
            type=self._validate_serializable_list_value,
            action="append",
            required=True,
            location="json",
            help="",
        )

    def post(self, id_: int) -> TopicModelStatusJson:
        args = self.reqparse.parse_args()
        topic_names: T.List[str] = args["topic_names"]
        topic_mdl = get_object_or_404(models.TopicModel, models.TopicModel.id_ == id_)

        self._validate_topic_model_finished_training(topic_mdl)
        if len(topic_names) != topic_mdl.num_topics:
            raise BadRequest(
                f"Topic model has {topic_mdl.num_topics} topics, but {len(topic_names)} topics were provided."
            )

        topic_mdl.topic_names = topic_names
        topic_mdl.save()
        return self._topic_model_status_json(topic_mdl)


class TopicModelsTopicsPreview(TopicModelRelatedResource):

    url = "/topic_models/<int:topic_model_id>/topics/preview"

    def get(self, topic_model_id: int) -> TopicModelPreviewJson:
        topic_mdl = get_object_or_404(
            models.TopicModel, models.TopicModel.id_ == topic_model_id
        )
        self._validate_topic_model_finished_training(topic_mdl)

        keywords_per_topic = self._get_keywords_per_topic(topic_mdl)
        examples_per_topic = self._get_examples_per_topic(topic_mdl)

        assert len(keywords_per_topic) == len(examples_per_topic)

        topic_mdl_status_json = self._topic_model_status_json(topic_mdl)
        topic_preview_json = TopicModelPreviewJson(
            {
                "topic_model_id": topic_mdl_status_json["topic_model_id"],
                "topic_model_name": topic_mdl_status_json["topic_model_name"],
                "num_topics": topic_mdl_status_json["num_topics"],
                "topic_names": topic_mdl_status_json["topic_names"],
                "notify_at_email": topic_mdl.notify_at_email,  # TODO: umm, why the black sheep?
                "status": topic_mdl_status_json["status"],
                "metrics": topic_mdl_status_json["metrics"],
                "topic_previews": [
                    OneTopicPreviewJson({"examples": examples, "keywords": keywords})
                    for examples, keywords in zip(
                        examples_per_topic, keywords_per_topic
                    )
                ],
            }
        )
        return topic_preview_json

    @staticmethod
    def _get_keywords_per_topic(topic_mdl: models.TopicModel) -> T.List[T.List[str]]:
        """

        Returns:
            keywords_per_topic: A list of lists of strings.
                List i contains keywords that have highest emission probability.
                under topic i.
        """

        # Look at the documentation at utils.Files.topic_model_keywords_file() for
        # what the file is supposed to look like.
        keywords_file_path = utils.Files.topic_model_keywords_file(topic_mdl.id_)

        keywords_df = pd.read_csv(keywords_file_path, index_col=0, header=0)
        keywords_df = keywords_df.iloc[:-1]  # Remove the "probabilities" row
        return keywords_df.T.values.tolist()  # type: ignore[no-any-return]

    @staticmethod
    def _get_examples_per_topic(topic_mdl: models.TopicModel) -> T.List[T.List[str]]:
        """

        Returns;
            topic_most_likely_examples: A list of list of strings.
                List i within this list contains examples whose most likely topic was
                determined to be topic i.

                i starts counting from zero. The maximum number of examples is determined
                by Settings.MAX_NUM_EXAMPLES_PER_TOPIC_IN_PREIVEW
        """

        # Look at the documentation at utils.Files.topic_model_topics_by_doc_file() for
        # what the file is supposed to look like.
        topics_by_doc_path = utils.Files.topic_model_topics_by_doc_file(topic_mdl.id_)
        topics_by_doc_df = pd.read_csv(topics_by_doc_path, index_col=0, header=0)  # type: ignore[attr-defined]
        bool_mask_topic_most_likely_examples: T.List[pd.Series[str]] = [
            topics_by_doc_df[Settings.MOST_LIKELY_TOPIC_COL] == topic_num
            for topic_num in range(topic_mdl.num_topics)
        ]
        examples_per_topic: T.List[T.List[str]] = [
            topics_by_doc_df.loc[bool_mask, Settings.CONTENT_COL][
                : Settings.MAX_NUM_EXAMPLES_PER_TOPIC_IN_PREIVEW
            ]
            .to_numpy()
            .tolist()
            for bool_mask in bool_mask_topic_most_likely_examples
        ]

        return examples_per_topic


class TopicModelsKeywords(TopicModelRelatedResource, SupportSpreadsheetFileType):
    url = "/topic_models/<int:topic_model_id>/keywords"

    def __init__(self) -> None:
        self.reqparse = reqparse.RequestParser()
        SupportSpreadsheetFileType.__init__(self)

    def get(self, topic_model_id: int) -> Response:
        topic_mdl = get_object_or_404(
            models.TopicModel, models.TopicModel.id_ == topic_model_id
        )
        if topic_mdl.lda_set is None:
            raise NotFound(
                "A training set has not been uploaded to this topic model yet. Please upload training data first."
            )
        if not topic_mdl.lda_set.lda_completed:
            raise BadRequest("Training this topic model has not been completed yet.")
        else:
            args = self.reqparse.parse_args()
            if topic_mdl.topic_names is not None:
                keywords_file = self._get_keywords_file_with_topic_names(topic_mdl)
            else:
                keywords_file = utils.Files.topic_model_keywords_file(topic_mdl.id_)

            file_type_with_dot = "." + args["file_type"]
            keywords_with_type_file = self._get_cached_version_with_file_type(
                keywords_file, file_type_with_dot
            )
            name_for_file = f"Topic_model_{topic_mdl.name}-keywords{file_type_with_dot}"
            return send_file(
                keywords_with_type_file,
                as_attachment=True,
                attachment_filename=name_for_file,
            )

    @classmethod
    def _get_keywords_file_with_topic_names(cls, topic_mdl: models.TopicModel) -> Path:
        topic_mdl = cls._ensure_topic_names(topic_mdl)

        keywords_file = utils.Files.topic_model_keywords_file(topic_mdl.id_)
        keywords_file_with_topic_names = utils.Files.topic_model_keywords_with_topic_names_file(
            topic_mdl.id_, topic_mdl.topic_names
        )
        if not keywords_file_with_topic_names.exists():
            keywords_df = pd.read_csv(keywords_file, header=0, index_col=0)
            # Sanity check
            pd.testing.assert_index_equal(
                keywords_df.columns,
                pd.Index([f"{i}" for i in range(topic_mdl.num_topics)]),
            )

            keywords_df.columns = pd.Index(topic_mdl.topic_names)
            keywords_df.to_csv(keywords_file_with_topic_names, header=True, index=True)

        return keywords_file_with_topic_names


class TopicModelsTopicsByDoc(TopicModelRelatedResource, SupportSpreadsheetFileType):
    url = "/topic_models/<int:topic_model_id>/topics_by_doc"

    def __init__(self) -> None:
        self.reqparse = reqparse.RequestParser()
        SupportSpreadsheetFileType.__init__(self)

    def get(self, topic_model_id: int) -> Response:
        topic_mdl = get_object_or_404(
            models.TopicModel, models.TopicModel.id_ == topic_model_id
        )
        if topic_mdl.lda_set is None:
            raise NotFound(
                "A training set has not been uploaded to this topic model yet. Please upload training data first."
            )
        if not topic_mdl.lda_set.lda_completed:
            raise BadRequest("Training this topic model has not been completed yet.")
        else:
            args = self.reqparse.parse_args()
            assert topic_mdl.topic_names is not None  # We assign default topic names
            topics_by_doc_file = self._get_topics_by_doc_file_with_topic_names(
                topic_mdl
            )
            file_type_with_dot = "." + args["file_type"]
            topics_by_doc_with_file_type = self._get_cached_version_with_file_type(
                topics_by_doc_file, file_type_with_dot
            )
            name_for_file = f"Topic_model_{topic_mdl.name}-keywords{file_type_with_dot}"
            return send_file(
                topics_by_doc_with_file_type,
                as_attachment=True,
                attachment_filename=name_for_file,
            )

    @classmethod
    def _get_topics_by_doc_file_with_topic_names(
        cls, topic_mdl: models.TopicModel
    ) -> Path:
        topic_mdl = cls._ensure_topic_names(topic_mdl)
        topics_by_doc_file_with_topic_names = utils.Files.topic_model_topics_by_doc_with_topic_names_file(
            topic_mdl.id_, topic_mdl.topic_names
        )
        if not topics_by_doc_file_with_topic_names.exists():
            topics_by_doc_file = utils.Files.topic_model_topics_by_doc_file(
                topic_mdl.id_
            )
            keywords_df = pd.read_csv(topics_by_doc_file, header=0, index_col=0)
            # Sanity check, the "default topic names" are "Topic 1", "Topic 2", etc.
            # Technically the i-th default topic name is
            # Settings.PROBAB_OF_TOPIC_TEMPLATE.format(str(i + 1))
            # where i starts from 0
            pd.testing.assert_index_equal(
                keywords_df.columns,
                pd.Index(
                    [Settings.CONTENT_COL, Settings.STEMMED_CONTENT_COL]
                    + [
                        Settings.PROBAB_OF_TOPIC_TEMPLATE.format(
                            Settings.DEFAULT_TOPIC_NAME_TEMPLATE.format(topic_num)
                        )
                        for topic_num in range(1, topic_mdl.num_topics + 1)
                    ]
                    + [Settings.MOST_LIKELY_TOPIC_COL]
                ),
            )

            keywords_df.columns = pd.Index(
                [Settings.CONTENT_COL, Settings.STEMMED_CONTENT_COL]
                + [
                    Settings.PROBAB_OF_TOPIC_TEMPLATE.format(topic)
                    for topic in topic_mdl.topic_names
                ]
                + [Settings.MOST_LIKELY_TOPIC_COL]
            )

            keywords_df.to_csv(
                topics_by_doc_file_with_topic_names, header=True, index=True
            )

        return topics_by_doc_file_with_topic_names


# We will initialize database manually in here, so we are not going to do
# db.may_need_database_init
@needs_settings_init()
def create_app(logging_level: int = logging.WARNING) -> Flask:
    """App factory to for easier testing. 
    Creates:
        PROJECT_DATA_DIRECTORY if it doesn't exist.

    Initializes:
        Sqlite database, if the SQLITE file doesn't exist yet.
        
    Returns:
        app: Flask() object.
    """
    logging.basicConfig()
    logger.setLevel(logging_level)

    # Usually, we'd read this from app.config, but we need it to create app.config ...
    app = Flask(__name__)

    app.config["SERVER_NAME"] = Settings.SERVER_NAME

    # Create project root if necessary
    if not Settings.PROJECT_DATA_DIRECTORY.exists():
        Settings.PROJECT_DATA_DIRECTORY.mkdir()
        utils.Files.supervised_dir(ensure_exists=True)
        utils.Files.unsupervised_dir(ensure_exists=True)

    Version.ensure_project_data_dir_version_safe()

    # Create database tables if the SQLITE file is going to be new
    if not Settings.DATABASE_FILE.exists():
        database = pw.SqliteDatabase(str(Settings.DATABASE_FILE))
        models.database_proxy.initialize(database)
        with models.database_proxy.connection_context():
            logger.info("Created tables because SQLITE file was not found.")
            models.database_proxy.create_tables(models.MODELS)
    else:
        database = pw.SqliteDatabase(str(Settings.DATABASE_FILE))
        models.database_proxy.initialize(database)
        logger.info("SQLITE file found. Not creating tables")

    app.queue_manager = QueueManager()

    @app.before_request
    def _db_connect() -> None:
        """Ensures that a connection is opened to handle queries by the request."""
        models.database_proxy.connect(reuse_if_open=True)

    @app.teardown_request
    def _db_close(exc: T.Optional[Exception]) -> None:
        """Close on tear down."""
        if not models.database_proxy.is_closed():
            models.database_proxy.close()

    api = Api(app)

    # Add commands
    database_commands.add_commands_to_app(app)

    lsresource_cls: T.Tuple[T.Type[ResourceProtocol], ...] = (
        Classifiers,
        OneClassifier,
        ClassifiersTrainingFile,
        ClassifiersTestSets,
        OneClassifierTestSet,
        ClassifiersTestSetsFile,
        ClassifiersTestSetsPredictions,
        TopicModels,
        OneTopicModel,
        TopicModelsTrainingFile,
        TopicModelsTopicsNames,
        TopicModelsTopicsPreview,
        TopicModelsTopicsByDoc,
        TopicModelsKeywords,
    )

    for resource_cls in lsresource_cls:
        assert (
            resource_cls.url[0] == "/"
        ), f"{resource_cls.__name__}.url must start with a /"
        url = API_URL_PREFIX + resource_cls.url
        # the "endpoint" makes it easier to use url_for() in unit testing
        api.add_resource(resource_cls, url, endpoint=resource_cls.__name__)

    return app


F = T.TypeVar("F", bound=T.Callable[..., T.Any])


def needs_app_context(func: F) -> F:
    """Decorator to ensure flask.current_app is set.

    Note that create_app itself @db.needs_database_init, which itself
    @settings.needs_settings_init(), so we're all set after calling this.
    """
    functools.wraps(func)

    def wrapper(*args: T.Any, **kwargs: T.Any) -> T.Any:
        ctx: T.Optional[T.Any] = None
        if not has_app_context():  # type: ignore[no-untyped-call]
            app = create_app()
            ctx = app.app_context()
            ctx.push()
        res = func(*args, **kwargs)
        if ctx is not None:
            ctx.pop()
        return res

    return T.cast(F, wrapper)
