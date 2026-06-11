> Archived from the community-maintained Picasa Resources site:
> <https://sites.google.com/site/picasaresources/picasa/release-notes>
> Retrieved 2026-06-11 (text extracted with trafilatura).

The release notes of the most recent version(s):

2016-02-09

Picasa (for Windows Version 3.9 Build 141.259)

This version is the very last version created by Google who stopped supporting it on March 15, 2016.

2015-10-09

Picasa (on Windows: in 3.9.141.255, on Mac: 3.9.141.???) and Google Photos Backup v1.1.1:

Patches for image handling (more RAW vulnerabilities)

Fix various reported crashes around the handling of attached devices in Google Photos Backup.

Ensure larger files are prioritized lower than smaller in Google Photos Backup.

Improve UI in display of failed files in Google Photos Backup.

Minor performance improvements in file process and upload in Google Photos Backup.

Support for the new Apple Photos library

Note: Help menu -> Check for Updates is still not functional.

2015-09-03

Picasa (on Windows: in 3.9.140.248, on Mac: 3.9.140.???):

the Shop button / Order prints function and the Configure Buttons tool weren't functional in build 239

Restored the Defaults.ini file that was missing from the Program Files (x86)\Google\Picasa3\ runtime\ folder.

Restored the Program Files (x86)\Google\Picasa3\buttons folder and it's core.pbz and geotag pbz files.

Network related fixes for OS X that affect both Google Photos Backup and Picasa.

Security Patches for RAW image handling Vulnerabilities.

Note: Help menu -> Check for Updates is still not functional.

2015-07-17

Picasa (on Windows: in 3.9.140.239, on Mac: 3.9.140.287):

Uploads & links

picasaweb.google.com no longer redirects to G+ Photos

For photos uploaded Google Photos, "view online" links will go to PicasaWeb, not G+ Photos

Text changes in popup for uploading and/or sharing

“Existing Google Photos album has X photo” -> “Existing Google Photos album has X photos and may already be shared”

When you click on "Upload to Google Photos," text on the screen has changed from "Upload to Google Photos and Share to G+” --> “Upload to Google Photos and Optionally Share to G+”?

“Add a message” --> “[Optional] Sharing to Google+? Add a message”

“+ Add circles or people…” --> “[Optional] Add Google+ circles or people to share with”

Main Picasa Screen

+ sign is removed from the camera icon on the green upload button

G+ button replaced with Google Photos at the top of the page

upload/sync of face tags to Google Photos

Upload/sync of face tags from Picasa has been disabled and the option to do so (Options, Google Photos tab) has been removed. Picasa can still tag faces locally but those tags will simply not be pushed online when a photo is uploaded.

*Note: the Shop button / Order prints function and the Configure Buttons tool aren't functional in build 239. Please Update ASAP.*

The Defaults.ini file is missing from the Program Files (x86)\Google\Picasa3\ runtime\ folder.

The Program Files (x86)\Google\Picasa3\buttons folder and it's core.pbz and geotag pbz files are missing.

2015-02-17

Picasa (on Windows: in 3.9.139.161, on Mac: 3.9.139.218):

Bug Fix: Fix several potential security flaws in handling of TIFF files to avoid possible exploitation should users obtain corrupt TIFF files from an online source and attempt to process them with either Picasa or Auto Backup.

2014-08-13

Picasa (on Windows: in 3.9.138.151, on Mac: 3.9.138.202):

When a video was uploaded to G+ Photos, the video file extension in G+ Photos became .JPG instead of it's original extension.

2014-08-12

Picasa (on Windows: in 3.9.138.150, on Mac: 3.9.138.201):

Bug Fix: Save button in Picasa's OneUp mode not properly enabled for large (>500 photos) folders/albums.

Bug Fix: Picasa was not properly refining images with pending edits in TwoUp (A|B) mode.

Bug Fix: Restore Picasa's automatic conversion to JPEG format on explicit export operations (including email).

Bug Fix: Fix broken video uploads in Picasa (incorrect image format for thumbnails).

Bug Fix: Fix the Print Contact Sheet utility in Picasa to display Album date, not current date/time.

Bug Fix: Fix Picasa's Save and Save As... operations were not preserving file extensions correctly.

Bug Fix: Fix Picasa's text wrapping in some cases which would break on the decimal point of floating point values.

Bug Fix: Prevent the ability to create folders with trailing spaces in their name during Picasa's Import utility.

Bug Fix: Remove the unused Picasa option to Upload people album thumbnails to Google Contacts.

Bug Fix: makernotes corruption for some Olympus and Kodak camera models.

Google+ auto backup for desktop (on Windows: 1.0.26.150, on Mac: 1.0.26.201)

Improvement: Increase maximum file size from 36MB to 50MB for AutoBackup.

Improvement: "Easter egg for debugging purposes": if you hold down the <SHIFT> key before clicking on the application icon an extra menu item will appear (not translated), File Status.... Selecting this command will bring up a dialog box allowing you to enter the full path to an image (or video) file and press the Get Status button. Both the application-local status of the file (whether it's been scanned, whether AB thinks it's been uploaded, etc.) AND the online status of the file will be displayed. If the file is found in the user's account online, a link to display it is also there in the dialog.

Bug Fix: AutoBackup for MacOS would sometimes get into a 100% CPU usage state.

Bug Fix: Proper display of Drive storage used when user has unlimited quota.

Bug Fix: Fixed crash in AutoBackup for Windows that could occur while uploading very large files.

2014-06-09

Picasa (on Windows: 3.9.137.141, on Mac: 3.9.137.192)

Improvement: Allow Picasa to upload full-size non-JPEG images without converting to JPEG.

Bug Fix: Prevent Picasa's album sync from overwriting animated GIFs in a synced album.

Google+ auto backup for desktop (on Windows: 1.0.25.141, on Mac: 1.0.25.184)

Improvement: Add About dialog box to AutoBackup so that current version is easily visible.

Improvement: AutoBackup network performance improvements for obtaining server-side configuration.

Bug Fix: Fixed crashes in AutoBackup for Windows during application shutdown.

Bug Fix: Fixed problems in AutoBackup with switching between full and standard size uploads and RAW support.

Bug Fix: Make sure AutoBackup runs automatically after installation.

Bug Fix: Address some minor UI problems in AutoBackup (text over-runs, typos).

Bug Fix: Fixed crash in AutoBackup for Mac during file scanning.

Bug Fix: AutoBackup for Mac would sometimes duplicate files imported from a media device.

2014-03-31

Google+ auto backup for desktop

Improvement: Preserve your original RAW files in Auto Backup, and you will also be able to download the RAW image once it’s been uploaded.

RAW upload for Auto Backup is opt out, as the setting will default to ON. You can change this setting: there will be an added checkbox in Auto Backup preferences with option “Upload full-size RAW files.” See attached image!

If the checkbox is on, RAW files are imported from memory cards the user plugs in.

Uploading full size or RAW (and any images larger than 2048 px) counts against your Drive storage quota.

Users will be notified of this change when they launch the new version of Auto Backup for desktop.

2014-03-10

Picasaweb/Picasa:

Bug Fix: edits to pictures in a Picasa album/folder with sync-to-web enabled weren't synced anymore.

2014-03-10

Picasa (on Windows: 3.9.137.118, on Mac: 3.9.137.?)

Bug Fix: Translation file included again in the installer.

Bug Fix: in some (edge) cases sync-to-web turned itself off .

2014-03-07

Picasa (on Windows: 3.9.137.114, on Mac: 3.9.137.163)

Improvement: Change Picasa from Maps v2 API to v3 API (this affects the Geo Panel in one-up mode). Basically the older, v2 interface is being deprecated.

Bug Fix: Fixes to the Windows WinINET handling which fixes the incompatibility issues with IE10 that would prevent logins from working.

Bug Fix: Fix the JavaScript error that was occurring during two-step authentication during logins.

Bug Fix: Fix for Picasa where album sync was being turned off by an incorrect determination of a face-tag upload error.

Google+ auto backup (on Windows: 1.0.23.114, on Mac: 1.0.23.53)

Improvement: 2x or better performance improvements in the uploads from Auto Backup.

Improvement: Auto Backup and Picasa installation changes.

Shortened link to this page: https://goo.gl/zjj784
