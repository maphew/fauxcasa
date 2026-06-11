> Archived from the community-maintained Picasa Resources site:
> <https://sites.google.com/site/picasaresources/picasa/move-photos-and-picasa-database-to-a-new-computor>
> Retrieved 2026-06-11 (text extracted with trafilatura).

If you are replacing your hard drive or are moving your Picasa program and photo and video collection to a new computer with the same Windows version in both the old and the new computer, you may be able to move the complete Picasa program files and photos without a time consuming database rebuild.

The techniques outlined here can be used to produce a backup of your photo collection. The directions are very complicated, so please read them carefully.

For the best backup, consider writing all Face Tags to the photo metadata as outlined in the Optional steps further below.

Make a backup of all photo folders as outlined in Step (1) below.

Make a backup of the Picasa database as outlined in Step (2) below.

Store the external drive in a safe place so your backup will be safe from fire, deletion, or theft.

If something unexpected happens to your photo collection and you need to restore it to the way it was when the backup was done, use the techniques outlined in steps (3), (4), (5) and (6) as necessary. If you lose one folder or photo, you can find it in the backup and copy it back to the photo collection on your computer.

_________________________________________________________________

WARNING: **These instructions will work only if both the old and the new computers have the same Windows version**. Example: both computers have Windows XP; or both can have other versions of Windows (such as Vista, Win7, Win8.1, and Win10) because they all use the same folder structure.

If the old computer has Windows XP and the new computer has Windows 7, 8, 10 or Vista, follow this link to move your pictures (not the Picasa database) to the new computer: Move photos to a new computer

__________________________________________________________________

Copying the photos and the Picasa database will only work if your full path to the photo folders is the same on the old and new computers:

The version of Windows on both machines must be compatible, either XP on both; or Windows 7, 8, 10, or Vista on both.

The User name must be the same on both because the user name is part of the path.

The path to the pictures folder must be the same on both.

Example: **C:\users\<user name>\Pictures** on Win7, Win8.1, Win10, or Vista

The path to any folders being watched outside of pictures must be the same

Example: **My Documents\Scans**

If there are photo folders on external drive(s), the drive letter and folder path must be the same

 Example: if the old drive path was **P:\Photos**, the drive must be mounted as P: on the new PC

We are recommending that all your face tags be written to the XMP metadata in the photos because they will better survive any future database rebuild. This is optional and can also be done after the transfer of photos to the new PC.

If you do want to be sure all the faces are in the Photo XMP metadata, do the following:

Go to the Tools menu then to Options; select the Name Tags tab and make sure the "Store name tags in photo" box is checked; click OK.

Go to the Tools menu then to Experimental; select "Write Faces to XMP..."

In the Write Faces box that comes up, select the Write Faces button.

Wait for quite a while depending on the number of Photos for all face tags to be written.

Exit Picasa to make sure the database is written to disk.

Note: Exit Picasa on the old PC and make sure you don't start it again until after you have copied all your photo folders and your database to the external drive. The reason for this is you want all the photo folders and the Picasa Database to match each other.

Make sure Windows File Explorer is set up to not Hide hidden files and folders, and to Show extensions for known file types. These settings are in the Windows File Explorer View Options menu depending on Windows version.

Make a folder on the external drive called something like "Photos" (don't call it Pictures or My Pictures to avoid confusion).

Select a few or many files and folders in the My Pictures / Picture folder, then right click on one of them and select **Copy.**

Go to the external drive and into the Photos folder and right-click in an empty area and select **Paste**.

Repeat the above step multiple times with remaining files and folders in My Pictures / Pictures until you have all them copied into the Photos Folder. The goal is for the Photos folder to contain a copy of every file and folder in My Pictures on the old PC.

Make a folder on the external drive called something like OtherPhotos

Copy any folders outside of My Pictures into OtherPhotos and either remember or recreate the whole folder path of these photo folders.

Example if you have a folder in Documents called "Old wedding photos," create a Documents folder inside OtherPhotos on the external drive, and copy the complete "Old wedding photos" folder to that new Documents folder. The goal is to have a complete copy of all Photo folders paths so they can be duplicated on the new PC.

If you have Photo folders on an external drive, and that drive is going to be transferred to the new PC, AND be assigned the same drive letter as it had on the old PC it is not necessary to copy those photo folders, but instead, transfer the drive to the new PC and mount it to the same drive letter during the paste process when called for below.

Make a folder called Photos Database or something like that on the external drive, and create an empty Google folder inside that.

Paste the appropriate path below (depending on your operating system) into the Windows File Explorer folder bar:

Windows XP:     **%userprofile%\Local Settings\Application Data\Google\**

Windows Vista / Windows 7 / Windows 8.1 / Windows 10:  **%LocalAppData%\Google\**

When the Google folder opens, you'll see two folders that have "Picasa" in the folder name: "**Picasa2**" and "**Picasa2Albums**

Copy those two complete folders to the external drive **\Photos Database\Google\** folder.

You should now have a complete backup of all your photos and their database. If you have missed any photos or folders, copy them to Photos or Other Photos right now. If you miss any folders or photos you can copy them later, but Picasa will have to re-index those folders when you copy them to the new PC.

Make sure the latest version of Picasa is installed, see "Picasa final version"

The latest and final version for Windows is: Picasa 3.9.141 build 259 and the latest version for Mac is: Picasa for Mac version 3.9

After installing, go to Tools > Options > General tab and make sure "Check for Updates" is **not **check marked. 

The version of Picasa is listed under the Help menu > About Picasa

If you have photos already on the new PC, make sure the Pictures folder doesn't have any folders with the same name as folders you are going to be pasting into it from the Backup set.

You CANNOT Merge folders with the same name at this point because you will be corrupting some hidden files.

If there are duplicate folder names in the new PC to the folders from the old PC you will be copying, either rename them here in Pictures by adding "NEW" to the folder name, or delete them if they are really duplicates of the folders to be pasted from the old PC.

After the complete process is done you can compare the NEW folders to the pasted folders with the same name and move photos between them using Picasa, then delete the NEW folder if not needed, or rename it to reflect its contents.

In the new PC Pictures folder before pasting, it is very Important that you Rename the Picasa subfolder to PicasaNEW so things like collages and movies if any on the new PC don't get confused with those to be pasted.

Paste all the photos and folders from the external drive Photos folder to the new PC My Pictures folder. If any folders complain about duplicates, remember the folder name and skip the paste of that folder. Later you will have to rename the duplicate folder in the new PC and re-paste the folder from the old PC to the new PC.

Also paste all the OtherPhotos folders to the new PC to the same paths as they were on the old PC. Create the path if necessary.

If any drives are going to be transferred to the new PC, install and mount those drives to the same drive letters as they were on the old PC.

Paste the appropriate path below (depending on your operating system) into the Windows Explorer Address box:

Windows XP:     **%userprofile%\Local Settings\Application Data\Google\**

Windows Vista / Windows 7 / Windows 8:  **%LocalAppData%\Google\**

In the new PC Google folder, if Picasa has been run before, you'll see two folders that have "Picasa" in the folder name: "**Picasa2**" and "**Picasa2Albums**

Rename those two folders by adding NEW to the folder name, so that you have a backup of the present database in case something goes wrong.

Copy the Picasa2 and Picasa2Albums folders from the external drive **\Photo Database\Google\ **folder to the Google folder above where you just renamed the old database.

Start Picasa on the new PC

Picasa should immediately show all the folders copied from the old PC and it will scan and show any new folders you renamed with NEW above.

If the new PC has any folders that were not in the old PC and are not in the watched folders list, go to Tools menu > Folder Manager.

In the Folder Manager, set any other folders you want to watch to "Scan Always".

In the Folder Manager, set any folders that were watched in the old PC that you don't want to watch in the new PC to "Remove from Picasa".

When you exit the Folder Manager by clicking OK, Picasa will scan any new photos and purge removed paths.

Go to Tools > Options > General tab and make sure "Check for Updates" is **not **check marked. 

The Pictures folder may have a folder called PicasaNEW. This folder contains any Collages, Movies, and Exports done on the new PC before this move/transfer operation. These collages, etc., should be moved to the Picasa folder using Picasa and the PicasaNEW folder should be deleted (or it can be renamed and remain as an archive).

Clean up any folders that have NEW added to the folder name by moving any photos you wish to keep to the correct folders.

Now that the new PC is operating correctly, please create a backup!
