"""Peewee database ORM."""
import enum
import typing as T

import peewee as pw  # type: ignore


_database = pw.SqliteDatabase("sqlite.db")
"""The database connection.

Ideally, this should depend on flask.current_app.config, but I don't know how to do
that.
"""


class BaseModel(pw.Model):
    """Defines metaclass with database connection."""

    class Meta:
        """meta class."""

        database = _database


# From: https://github.com/coleifer/peewee/issues/630
class EnumField(pw.CharField):
    """An EnumField for Peewee."""

    def __init__(
        self, enum_class: T.Type[enum.Enum], *args: T.Any, **kwargs: T.Any
    ) -> None:
        """init.

        Args:
            enum_class:
            *args: Passed to pw.CharField.
            *kwargs: Passed to pw.CharField.
        """
        self._enum_class = enum_class
        super().__init__(*args, **kwargs)

    def db_value(self, value: enum.Enum) -> str:
        """Convert enum to str."""
        return value.name

    def python_value(self, value: T.Any) -> enum.Enum:
        """Convert str to enum."""
        return self._enum_class(value)


class ListField(pw.TextField):
    """A field to facilitate storing lists of strings as a textfield."""

    def __init__(self, sep: str = ",", *args: T.Any, **kwargs: T.Any) -> None:
        """init.

        Args:
            sep: What separator to use to separate fields.
            *args: Passed to pw.CharField.
            *kwargs: Passed to pw.CharField.
        """
        assert len(sep) == 1
        self._sep = sep
        super().__init__(*args, **kwargs)

    def db_value(self, value: T.Any) -> str:
        """Validate and convert to string."""
        if not isinstance(value, list) or set(map(type, value)) != {str}:
            raise ValueError("ListField stores lists of strings.")

        if any(self._sep in item for item in value):
            raise ValueError(
                f"ListField has separator {self._sep}, so a list item"
                " cannot have this character."
            )
        return self._sep.join(value)

    def python_value(self, value: str) -> T.List[str]:
        """Convert str to list."""
        return value.split(self._sep)


class ProgressEnum(str, enum.Enum):
    """Progress field for Classifier.

    The inheritance from str is to support json serialization.
    """

    NOT_TRAINED = "NOT_TRAINED"
    TRAINING = "TRAINING"
    RUNNING_INFERENCE = "RUNNING_INFERENCE"
    DONE = "DONE"


class Metrics(BaseModel):
    """Metrics on a labeled set.

    Attributes:
        macro_f1:
        macro_precision:
        macro_recall:
        accuracy:
    """

    macro_f1 = pw.FloatField()
    macro_precision = pw.FloatField()
    macro_recall = pw.FloatField()
    accuracy = pw.FloatField()


class LabeledSet(BaseModel):
    """This is either a training set, or a test set.

    We don't need a "name" field for this because there will only be one training set
    and one test set per classifier. For the same reason, we don't store a foreign key
    to the classifier here.

    Attributes:
        id: set id.
        file_path: file path relative to classifier file path.
        training_or_inference_completed: Whether the training or the inference has
            completed this set.
        metrics: Metrics on set. Can be null in the case of a training set.
    """

    id = pw.AutoField(primary_key=True)
    file_path = pw.CharField(null=False)
    training_or_inference_completed = pw.BooleanField()
    metrics = pw.ForeignKeyField(Metrics, null=True)


class Classifier(BaseModel):
    """.

    Attributes:
        name: Name of classiifer.
        category_names: Comma separated names of categories. Means category names can't
            have commas.
        dir_path: Path where classifier related files (models, training set, dev set,
            test sets) are stored.
        trained_by_openFraming: Whether this is a classifier that openFraming provides,
            or a user trained.
        training_completed: Whether training was completed for classifer.
        training_set: The training set for classififer.
        test_set: The test set for classififer.
    """

    classifier_id = pw.AutoField(primary_key=True)
    name = pw.TextField()
    category_names = ListField()
    dir_path = pw.CharField()
    trained_by_openFraming = pw.BooleanField(default=False)
    training_set = pw.ForeignKeyField(LabeledSet, null=True)
    test_set = pw.ForeignKeyField(LabeledSet, null=True)


class UnlabelledSet(BaseModel):
    """This will be a prediction set.

    Attributes:
        id: set id.
        classifier: The classifier this set is intended for.
        name: User given name of the set.
        file_path: file path relative to classifier file path.
        inference_completed: Whether the training or the inference has
            completed this set.
    """

    id = pw.AutoField(primary_key=True)
    classifier = pw.ForeignKeyField(Classifier)
    name = pw.CharField()
    file_path = pw.CharField(null=False)
    inference_completed = pw.BooleanField()


def _create_tables() -> None:
    with database:
        database.create_tables(BaseModel.__subclasses__())


if __name__ == "__main__":
    _create_tables()
