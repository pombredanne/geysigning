#!/usr/bin/env python

# FIXME:
# Extract all use cases where gpg support comes from monkeysign and
# add them to this file.

import logging
from string import Template

import shutil
from tempfile import NamedTemporaryFile

from monkeysign.gpg import Keyring, TempKeyring


log = logging.getLogger()

### From Sections.py ###

def UIDExport(uid, keydata):
    """Export only the UID of a key.
    Unfortunately, GnuPG does not provide smth like
    --export-uid-only in order to obtain a UID and its
    signatures."""
    tmp = TempKeyring()
    # Hm, apparently this needs to be set, otherwise gnupg will issue
    # a stray "gpg: checking the trustdb" which confuses the gnupg library
    tmp.context.set_option('always-trust')
    tmp.import_data(keydata)
    for fpr, key in tmp.get_keys(uid).items():
        for u in key.uidslist:
            key_uid = u.uid
            if key_uid != uid:
                log.info('Deleting UID %s from key %s', key_uid, fpr)
                tmp.del_uid(fingerprint=fpr, pattern=key_uid)
    only_uid = tmp.export_data(uid)

    return only_uid


def MinimalExport(keydata):
    '''Returns the minimised version of a key

    For now, you must provide one key only.'''
    tmpkeyring = TempKeyring()
    ret = tmpkeyring.import_data(keydata)
    log.debug("Returned %s after importing %s", ret, keydata)
    assert ret
    tmpkeyring.context.set_option('export-options', 'export-minimal')
    keys = tmpkeyring.get_keys()
    log.debug("Keys after importing: %s (%s)", keys, keys.items())
    # We assume the keydata to contain one key only
    fingerprint, key = keys.items()[0]
    stripped_key = tmpkeyring.export_data(fingerprint)
    return stripped_key


def GetNewKeyring():
    return Keyring()

def GetNewTempKeyring():
    return TempKeyring()

class TempKeyringCopy(TempKeyring):
    """A temporary keyring which uses the secret keys of a parent keyring

    It mainly copies the public keys from the parent keyring to this temporary
    keyring and sets this keyring up such that it uses the secret keys of the
    parent keyring.
    """
    def __init__(self, keyring, *args, **kwargs):
        self.keyring = keyring
        # Not a new style class...
        if issubclass(self.__class__, object):
            super(TempKeyringCopy, self).__init__(*args, **kwargs)
        else:
            TempKeyring.__init__(self, *args, **kwargs)

        self.log = logging.getLogger()

        tmpkeyring = self
        # Copy and paste job from monkeysign.ui.prepare
        tmpkeyring.context.set_option('secret-keyring', keyring.homedir + '/secring.gpg')

        # copy the gpg.conf from the real keyring
        try:
            from_ = keyring.homedir + '/gpg.conf'
            to_ = tmpkeyring.homedir
            shutil.copy(from_, to_)
            self.log.debug('copied your gpg.conf from %s to %s', from_, to_)
        except IOError as e:
            # no such file or directory is alright: it means the use
            # has no gpg.conf (because we are certain the temp homedir
            # exists at this point)
            if e.errno != 2:
                pass


        # Copy the public parts of the secret keys to the tmpkeyring
        signing_keys = []
        for fpr, key in keyring.get_keys(None, secret=True, public=False).items():
            if not key.invalid and not key.disabled and not key.expired and not key.revoked:
                signing_keys.append(key)
                tmpkeyring.import_data (keyring.export_data (fpr))


## Monkeypatching to get more debug output
import monkeysign.gpg
bc = monkeysign.gpg.Context.build_command
def build_command(*args, **kwargs):
    ret = bc(*args, **kwargs)
    #log.info("Building command %s", ret)
    log.debug("Building cmd: %s", ' '.join(["'%s'" % c for c in ret]))
    return ret
monkeysign.gpg.Context.build_command = build_command



### Below are wrappers for gpg use cases that might be good to have in our
### implementation

def keyring_set_option(keyring, option, value = None):
    try:
        if option in keyring.context.options:
            keyring.context.set_option(option, value)

    except AttributeError:
        log.error("Object %s has no attribute context", keyring)
    except TypeError:
        log.error("Object %s is not a Keyring type", keyring)


def keyring_call_command(keyring, command, stdin = None):
    try:
        if option in keyring.context.options:
            keyring.context.call_command(command, stdin)

    except AttributeError:
        log.error("Object %s has no attribute context", keyring)
    except TypeError:
        log.error("Object %s is not a Keyring type", keyring)


def keyring_import_data(keyring, data):
    if keyring.import_data(data):
        imported_key_fpr = keyring.get_keys().keys()[0]
        log.debug("Imported data with fpr:\n%s", imported_key_fpr)

    else:
        log.error("Couldn't import data:\n%s", data)


def keyring_export_data(keyring, keyid):
    keys = keyring.get_keys(keyid)
    key = keys.values()[0]

    keyring.export_data(key.fpr, secret=False)
    keydata = keyring.context.stdout
    return keydata


if __name__ == '__main__':
    keyring = GetKeyringCopy()
    print keyring.get_keys()