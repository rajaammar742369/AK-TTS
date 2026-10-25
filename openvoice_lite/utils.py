import json


class HParams:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if isinstance(v, dict):
                v = HParams(**v)
            setattr(self, k, v)

    def __contains__(self, key):
        return key in self.__dict__

    def __getitem__(self, key):
        return getattr(self, key)

    def keys(self):
        return self.__dict__.keys()

    def items(self):
        return self.__dict__.items()


def get_hparams_from_file(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        return HParams(**json.load(f))
