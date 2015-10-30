def recursive_contains(key, data, compare_value=False):
    """
    Evaluates how often the key is a key (or value with lists) in data (and it's sub lists or dicts)-
    :param key: string
    :param compare_value: also compare non keys with the passed key
    """
    summed = 0
    if compare_value and ((key is data and type(data) is not str) or key is data):
        summed = 1
    sums = [0]
    if type(data) is dict:
        sums = [recursive_contains(key, data[sub], compare_value) for sub in data.keys()]
        if key in data.keys():
            summed += 1
    elif type(data) is list:
        sums = [recursive_contains(key, sub, compare_value) for sub in data]
    return summed + sum(sums)

def recursive_get(data, key):
    """
    Searches the value of the first key in the nested data dict, that has the given name.
    :param data: data dict
    :param key: given key
    :return: value of None if no value is found
    """
    if type(data) is not dict:
        return None
    if key in data.keys():
        return data[key]
    for sub in data.keys():
        sub_val = recursive_get(data[sub], key)
        if sub_val is not None:
            return sub_val
    return None

def recursive_find_key(key, data):
    """
    Return a list of keys that lead in the data tree to the first key with the given name.
    :param data: data dict
    :param key: given key
    :return: list of keys or None if the data tree has no such key
    """
    if type(data) is not dict:
        return None
    if key in data.keys():
        return [key]
    for sub in data.keys():
        sub_keys = recursive_find_key(key, data[sub])
        if sub_keys is not None:
            return [sub] + sub_keys
    return None


def recursive_exec_for_leafs(data: dict, func, _path_prep=[]):
    """
    Executes the function for every leaf key (a key without any sub keys) of the data dict tree.
    :param data: dict tree
    :param func: function that gets passed the leaf key, the key path and the actual value
    """
    if type(data) is not dict:
        return
    for subkey in data.keys():
        if type(data[subkey]) is dict:
            recursive_exec_for_leafs(data[subkey], func, _path_prep=_path_prep + [subkey])
        else:
            func(subkey, _path_prep + [subkey], data[subkey])


class Singleton(type):
    """ Singleton meta class.
    See http://stackoverflow.com/a/6798042
    """
    _instances = {}
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]