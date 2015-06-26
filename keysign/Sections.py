#!/usr/bin/env python
#    Copyright 2014 Andrei Macavei <andrei.macavei89@gmail.com>
#    Copyright 2014 Tobias Mueller <muelli@cryptobitch.de>
#
#    This file is part of GNOME Keysign.
#
#    GNOME Keysign is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    GNOME Keysign is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with GNOME Keysign.  If not, see <http://www.gnu.org/licenses/>.

import logging
from urlparse import ParseResult
from string import Template
import shutil
from subprocess import call
from tempfile import NamedTemporaryFile

import requests
from requests.exceptions import ConnectionError

import sys

from monkeysign.ui import MonkeysignUi
from keysign.gpg.gpg import UIDExport, MinimalExport, GetNewKeyring, GetNewTempKeyring, TempKeyringCopy
from keysign.gpg.gpg import UIDExport_gpgme

from compat import gtkbutton
import Keyserver
from KeysPage import KeysPage
from KeyPresent import KeyPresentPage
from SignPages import KeyDetailsPage
from SignPages import ScanFingerprintPage, SignKeyPage, PostSignPage
import MainWindow

import key

from gi.repository import Gst, Gtk, GLib
# Because of https://bugzilla.gnome.org/show_bug.cgi?id=698005
from gi.repository import GdkX11
# Needed for window.get_xid(), xvimagesink.set_window_handle(), respectively:
from gi.repository import GstVideo


Gst.init([])


FPR_PREFIX = "OPENPGP4FPR:"
progress_bar_text = ["Step 1: Scan QR Code or type fingerprint and click on 'Download' button",
                     "Step 2: Compare the received fpr with the owner's fpr and click 'Sign'",
                     "Step 3: Key was succesfully signed and an email was send to owner."]


SUBJECT = 'Your signed key $fingerprint'
BODY = '''Hi $uid,


I have just signed your key

      $fingerprint


Thanks for letting me sign your key!

--
GNOME Keysign
'''




class KeySignSection(Gtk.VBox):

    def __init__(self, app):
        '''Initialises the section which lets the user
        choose a key to be signed by other person.

        ``app'' should be the "app" itself. The place
        which holds global app data, especially the discovered
        clients on the network.
        '''
        super(KeySignSection, self).__init__()

        self.app = app
        self.log = logging.getLogger()
        self.keyring = GetNewKeyring()

        # these are needed later when we need to get details about
        # a selected key
        self.keysPage = KeysPage()
        self.keysPage.connect('key-selection-changed',
            self.on_key_selection_changed)
        self.keysPage.connect('key-selected', self.on_key_selected)
        self.keyDetailsPage = KeyDetailsPage()
        self.keyPresentPage = KeyPresentPage()


        # create back button
        self.backButton = Gtk.Button('Back')
        self.backButton.set_image(Gtk.Image.new_from_icon_name("go-previous", Gtk.IconSize.BUTTON))
        self.backButton.set_always_show_image(True)
        self.backButton.connect('clicked', self.on_button_clicked)

        # set up notebook container
        self.notebook = Gtk.Notebook ()
        self.notebook.append_page (self.keysPage, None)
        vbox = Gtk.VBox ()
        # We place the button at the top, but that might not be the
        # smartest thing to do. Feel free to rearrange
        # FIXME: Consider a GtkHeaderBar for the application
        vbox.pack_start (self.backButton, False, False, 0)
        vbox.pack_start (self.keyPresentPage, True, True, 10)
        self.notebook.append_page (vbox, None)
        self.notebook.set_show_tabs (False)

        self.pack_start(self.notebook, True, True, 0)

        # this will hold a reference to the last key selected
        self.last_selected_key = None

        # When obtaining a key is successful,
        # it will save the key data in this field
        self.received_key_data = None


    def on_key_selection_changed(self, pane, keyid):
        '''This callback is attached to the signal which is emitted
        when the user changes their selection in the list of keys
        '''
        pass


    def on_key_selected(self, pane, keyid):
        '''This is the callback for when the user has committed
        to a key, i.e. the user has made a selection and wants to
        advance the program.
        '''
        self.log.info('User selected key %s', keyid)

        key = self.keyring.get_keys(keyid).values()[0]

        keyid = key.keyid()
        fpr = key.fpr
        self.keyring.export_data(fpr, secret=False)
        keydata = self.keyring.context.stdout

        self.log.debug("Keyserver switched on! Serving key with fpr: %s", fpr)
        self.app.setup_server(keydata, fpr)

        self.switch_to_key_present_page(key)


    def switch_to_key_present_page(self, key):
        '''This switches the notebook to the page which
        presents the information that is needed to securely
        transfer the keydata, i.e. the fingerprint and its barcode.
        '''
        self.keyPresentPage.display_fingerprint_qr_page(key)
        self.notebook.next_page()
        # This is more of a crude hack. Once the next page is presented,
        # the back button has the focus. This is not desirable because
        # you will go back when accidentally pressing space or enter.
        self.keyPresentPage.fingerprintLabel.grab_focus()
        # FIXME: we better use set_current_page, but that requires
        # knowing which page our desired widget is on.
        # FWIW: A headerbar has named pages.


    def on_next_button_clicked(self, button):
        '''A helper for legacy reasons to enable a next button

        All it does is retrieve the selection from the TreeView and
        call the signal handler for when the user committed to a key
        '''
        name, email, keyid = self.keysPage.get_items_from_selection()
        return self.on_key_selected(button, keyid)


    def on_button_clicked(self, button):

        page_index = self.notebook.get_current_page()

        if button == self.backButton:

            if page_index == 1:
                self.log.debug("Keyserver switched off")
                self.app.stop_server()

            self.notebook.prev_page()


class GetKeySection(Gtk.VBox):

    def __init__(self, app):
        '''Initialises the section which lets the user
        start signing a key.

        ``app'' should be the "app" itself. The place
        which holds global app data, especially the discovered
        clients on the network.
        '''
        super(GetKeySection, self).__init__()

        self.app = app
        self.log = logging.getLogger()

        # the temporary keyring we operate in
        self.tmpkeyring = None

        self.scanPage = ScanFingerprintPage()
        self.signPage = SignKeyPage()
        # set up notebook container
        self.notebook = Gtk.Notebook()
        self.notebook.append_page(self.scanPage, None)
        self.notebook.append_page(self.signPage, None)
        self.notebook.append_page(PostSignPage(), None)
        self.notebook.set_show_tabs(False)

        # set up the progress bar
        self.progressBar = Gtk.ProgressBar()
        self.progressBar.set_text(progress_bar_text[0])
        self.progressBar.set_show_text(True)
        self.progressBar.set_fraction(1.0/3)

        self.nextButton = Gtk.Button('Next')
        self.nextButton.connect('clicked', self.on_button_clicked)
        self.nextButton.set_image(Gtk.Image.new_from_icon_name("go-next", Gtk.IconSize.BUTTON))
        self.nextButton.set_always_show_image(True)

        self.backButton = Gtk.Button('Back')
        self.backButton.connect('clicked', self.on_button_clicked)
        self.backButton.set_image(Gtk.Image.new_from_icon_name('go-previous', Gtk.IconSize.BUTTON))
        self.backButton.set_always_show_image(True)

        bottomBox = Gtk.HBox()
        bottomBox.pack_start(self.progressBar, True, True, 0)
        bottomBox.pack_start(self.backButton, False, False, 0)
        bottomBox.pack_start(self.nextButton, False, False, 0)

        self.pack_start(self.notebook, True, True, 0)
        self.pack_start(bottomBox, False, False, 0)

        # We *could* overwrite the on_barcode function, but
        # let's rather go with a GObject signal
        #self.scanFrame.on_barcode = self.on_barcode
        self.scanPage.scanFrame.connect('barcode', self.on_barcode)
        #GLib.idle_add(        self.scanFrame.run)

        # A list holding references to temporary files which should probably
        # be cleaned up on exit...
        self.tmpfiles = []

    def set_progress_bar(self):
        page_index = self.notebook.get_current_page()
        self.progressBar.set_text(progress_bar_text[page_index])
        self.progressBar.set_fraction((page_index+1)/3.0)


    def strip_fingerprint(self, input_string):
        '''Strips a fingerprint of any whitespaces and returns
        a clean version. It also drops the "OPENPGP4FPR:" prefix
        from the scanned QR-encoded fingerprints'''
        # The split removes the whitespaces in the string
        cleaned = ''.join(input_string.split())

        if cleaned.upper().startswith(FPR_PREFIX.upper()):
            cleaned = cleaned[len(FPR_PREFIX):]

        self.log.warning('Cleaned fingerprint to %s', cleaned)
        return cleaned


    def on_barcode(self, sender, barcode, message=None):
        '''This is connected to the "barcode" signal.
        The message argument is a GStreamer message that created
        the barcode.'''

        fpr = self.strip_fingerprint(barcode)

        if fpr != None:
            try:
                pgpkey = key.Key(fpr)
            except key.KeyError:
                self.log.exception("Could not create key from %s", barcode)
            else:
                self.log.info("Barcode signal %s %s" %( pgpkey.fingerprint, message))
                self.on_button_clicked(self.nextButton, pgpkey, message)
        else:
            self.log.error("data found in barcode does not match a OpenPGP fingerprint pattern: %s", barcode)


    def download_key_http(self, address, port):
        url = ParseResult(
            scheme='http',
            # This seems to work well enough with both IPv6 and IPv4
            netloc="[[%s]]:%d" % (address, port),
            path='/',
            params='',
            query='',
            fragment='')
        return requests.get(url.geturl()).text

    def try_download_keys(self, clients):
        for client in clients:
            self.log.debug("Getting key from client %s", client)
            name, address, port, fpr = client
            try:
                keydata = self.download_key_http(address, port)
                yield keydata
            except ConnectionError, e:
                # FIXME : We probably have other errors to catch
                self.log.exception("While downloading key from %s %i",
                                    address, port)

    def verify_downloaded_key(self, downloaded_data, fingerprint):
        # FIXME: implement a better and more secure way to verify the key
        if self.tmpkeyring.import_data(downloaded_data):
            imported_key_fpr = self.tmpkeyring.get_keys().keys()[0]
            if imported_key_fpr == fingerprint:
                result = True
            else:
                self.log.info("Key does not have equal fp: %s != %s", imported_key_fpr, fingerprint)
                result = False
        else:
            self.log.info("Failed to import downloaded data")
            result = False

        self.log.debug("Trying to validate %s against %s: %s", downloaded_data, fingerprint, result)
        return result

    def sort_clients(self, clients, selected_client_fpr):
        key = lambda client: client[3]==selected_client_fpr
        client = sorted(clients, key=key, reverse=True)
        self.log.info("Check if list is sorted '%s'", clients)
        return clients

    def obtain_key_async(self, fingerprint, callback=None, data=None, error_cb=None):
        other_clients = self.app.discovered_services
        self.log.debug("The clients found on the network: %s", other_clients)

        #FIXME: should we create a new TempKeyring for each key we want
        # to sign it ?
        self.tmpkeyring = GetNewTempKeyring()

        other_clients = self.sort_clients(other_clients, fingerprint)

        for keydata in self.try_download_keys(other_clients):
            if self.verify_downloaded_key(keydata, fingerprint):
                is_valid = True
            else:
                is_valid = False

            if is_valid:
                # FIXME: make it to exit the entire process of signing
                # if fingerprint was different ?
                break
        else:
            self.log.error("Could not find fingerprint %s " +\
                           "with the available clients (%s)",
                           fingerprint, other_clients)
            self.log.debug("Calling error callback, if available: %s",
                            error_cb)

            if error_cb:
                GLib.idle_add(error_cb, data)
            # FIXME : don't return here
            return

        self.log.debug('Adding %s as callback', callback)
        GLib.idle_add(callback, fingerprint, keydata, data)

        # If this function is added itself via idle_add, then idle_add will
        # keep adding this function to the loop until this func ret False
        return False



    def sign_key_async(self, fingerprint, callback=None, data=None, error_cb=None):
        self.log.debug("I will sign key with fpr {}".format(fingerprint))

        keyring = GetNewKeyring()
        keyring.context.set_option('export-options', 'export-minimal')

        tmpkeyring = TempKeyringCopy(keyring)

        # 1. fetch the key into a temporary keyring
        # 1.a) from the local keyring
        # FIXME: WTF?! How would the ring enter the keyring in first place?!
        keydata = data or self.received_key_data

        if keydata:
            stripped_key = MinimalExport(keydata)
        else: # Do we need this branch at all?
            self.log.debug("looking for key %s in your keyring", fingerprint)
            keyring.context.set_option('export-options', 'export-minimal')
            stripped_key = keyring.export_data(fingerprint)

        self.log.debug('Trying to import key\n%s', stripped_key)
        if tmpkeyring.import_data(stripped_key):
            # 3. for every user id (or all, if -a is specified)
            # 3.1. sign the uid, using gpg-agent
            keys = tmpkeyring.get_keys(fingerprint)
            self.log.info("Found keys %s for fp %s", keys, fingerprint)
            assert len(keys) == 1, "We received multiple keys for fp %s: %s" % (fingerprint, keys)
            key = keys[fingerprint]
            uidlist = key.uidslist

            # FIXME: For now, we sign all UIDs. This is bad.
            ret = tmpkeyring.sign_key(uidlist[0].uid, signall=True)
            self.log.info("Result of signing %s on key %s: %s", uidlist[0].uid, fingerprint, ret)


            for uid in uidlist:
                uid_str = uid.uid
                self.log.info("Processing uid %s %s", uid, uid_str)

                # 3.2. export and encrypt the signature
                # 3.3. mail the key to the user
                signed_key = UIDExport(uid_str, tmpkeyring.export_data(uid_str))
                self.log.info("Exported %d bytes of signed key", len(signed_key))
                # self.signui.tmpkeyring.context.set_option('armor')
                tmpkeyring.context.set_option('always-trust')
                encrypted_key = tmpkeyring.encrypt_data(data=signed_key, recipient=uid_str)

                keyid = str(key.keyid())
                ctx = {
                    'uid' : uid_str,
                    'fingerprint': fingerprint,
                    'keyid': keyid,
                }
                # We could try to dir=tmpkeyring.dir
                # We do not use the with ... as construct as the
                # tempfile might be deleted before the MUA had the chance
                # to get hold of it.
                # Hence we reference the tmpfile and hope that it will be properly
                # cleaned up when this object will be destroyed...
                tmpfile = NamedTemporaryFile(prefix='gnome-keysign-', suffix='.asc')
                self.tmpfiles.append(tmpfile)
                filename = tmpfile.name
                self.log.info('Writing keydata to %s', filename)
                tmpfile.write(encrypted_key)
                # Interesting, sometimes it would not write the whole thing out,
                # so we better flush here
                tmpfile.flush()
                # As we're done with the file, we close it.
                #tmpfile.close()

                subject = Template(SUBJECT).safe_substitute(ctx)
                body = Template(BODY).safe_substitute(ctx)
                self.email_file (to=uid_str, subject=subject,
                                 body=body, files=[filename])


            # FIXME: Can we get rid of self.tmpfiles here already? Even if the MUA is still running?


            # 3.4. optionnally (-l), create a local signature and import in
            # local keyring
            # 4. trash the temporary keyring


        else:
            self.log.error('data found in barcode does not match a OpenPGP fingerprint pattern: %s', fingerprint)
            if error_cb:
                GLib.idle_add(error_cb, data)

        return False


    def send_email(self, fingerprint, *data):
        self.log.exception("Sending email... NOT")
        return False

    def email_file(self, to, from_=None, subject=None,
                   body=None,
                   ccs=None, bccs=None,
                   files=None, utf8=True):
        cmd = ['xdg-email']
        if utf8:
            cmd += ['--utf8']
        if subject:
            cmd += ['--subject', subject]
        if body:
            cmd += ['--body', body]
        for cc in ccs or []:
            cmd += ['--cc', cc]
        for bcc in bccs or []:
            cmd += ['--bcc', bcc]
        for file_ in files or []:
            cmd += ['--attach', file_]

        cmd += [to]

        self.log.info("Running %s", cmd)
        retval = call(cmd)
        return retval


    def on_button_clicked(self, button, *args, **kwargs):

        if button == self.nextButton:
            self.notebook.next_page()
            self.set_progress_bar()

            page_index = self.notebook.get_current_page()
            if page_index == 1:
                if args:
                    # If we call on_button_clicked() from on_barcode()
                    # then we get extra arguments
                    pgpkey = args[0]
                    message = args[1]
                    fingerprint = pgpkey.fingerprint
                else:
                    raw_text = self.scanPage.get_text_from_textview()
                    fingerprint = self.strip_fingerprint(raw_text)

                    if fingerprint == None:
                        self.log.error("The fingerprint typed was wrong."
                        " Please re-check : {}".format(raw_text))
                        # FIXME: make it to stop switch the page if this happens
                        return

                # save a reference to the last received fingerprint
                self.last_received_fingerprint = fingerprint

                # error callback function
                err = lambda x: self.signPage.mainLabel.set_markup('<span size="15000">'
                        'Error downloading key with fpr\n{}</span>'
                        .format(fingerprint))
                # use GLib.idle_add to use a separate thread for the downloading of
                # the keydata
                GLib.idle_add(self.obtain_key_async, fingerprint, self.recieved_key,
                        fingerprint, err)


            if page_index == 2:
                # self.received_key_data will be set by the callback of the
                # obtain_key function. At least it should...
                # The data flow isn't very nice. It probably needs to be redone...
                GLib.idle_add(self.sign_key_async, self.last_received_fingerprint,
                    self.send_email, self.received_key_data)


        elif button == self.backButton:
            self.notebook.prev_page()
            self.set_progress_bar()


    def recieved_key(self, fingerprint, keydata, *data):
        self.received_key_data = keydata
        openpgpkey = self.tmpkeyring.get_keys(fingerprint).values()[0]
        self.signPage.display_downloaded_key(openpgpkey, fingerprint)




class SignUi(MonkeysignUi):
    """sign a key in a safe fashion.

This program assumes you have gpg-agent configured to prompt for
passwords."""

    def __init__(self, app, args = None):
        super(SignUi, self).__init__(args)

        self.app = app


    def main(self):

        MonkeysignUi.main(self)

    def yes_no(self, prompt, default = None):
        # dialog = Gtk.MessageDialog(self.app.window, 0, Gtk.MessageType.INFO,
        #             Gtk.ButtonsType.YES_NO, prompt)
        # response = dialog.run()
        # dialog.destroy()

        # return response == Gtk.ResponseType.YES
        # Simply return True for now
        return True

    def choose_uid(self, prompt, key):
        # dialog = Gtk.Dialog(prompt, self.app.window, 0,
        #         (Gtk.STOCK_CANCEL, Gtk.ResponseType.REJECT,
        #          Gtk.STOCK_OK, Gtk.ResponseType.ACCEPT))

        # label = Gtk.Label(prompt)
        # dialog.vbox.pack_start(label, False, False, 0)
        # label.show()

        # self.uid_radios = None
        # for uid in key.uidslist:
        #     r = Gtk.RadioButton.new_with_label_from_widget(
        #                 self.uid_radios, uid.uid)
        #     r.show()
        #     dialog.vbox.pack_start(r, False, False, 0)

        #     if self.uid_radios is None:
        #         self.uid_radios = r
        #         self.uid_radios.set_active(True)
        #     else:
        #         self.uid_radios.set_active(False)

        # response = dialog.run()

        # label = None
        # if response == Gtk.ResponseType.ACCEPT:
        #     self.app.log.info("okay signing")
        #     label = [ r for r in self.uid_radios.get_group() if r.get_active()][0].get_label()
        # else:
        #     self.app.log.info('user denied signature')

        # dialog.destroy()
        # return label
        return None
