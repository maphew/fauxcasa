> Archived from the community-maintained Picasa Resources site:
> <https://sites.google.com/site/picasaresources/picasa/how-to-find-missing-photos>
> Retrieved 2026-06-11 (text extracted with trafilatura).

**Picasa's missing photos troubleshooter**

In Picasa, click the **Tools** menu.

Select **Options**.

Click the **File Types** tab.

Verify that you've selected the checkboxes for all the formats you'd like Picasa to display.

See the list of Supported file types

Click **OK**.

CMYK, unflattened, and transparent images don't display properly in Picasa.

Some Video types require 3rd party Codecs,

See Missing Videos topic

Picasa "Watches" the folders you designate in Folder Manager.

It scans the folders and indexes all pictures it finds in those folders into the Picasa "Database"

Picasa also builds a set of thumbnails in the database so it can instantly show the photo thumbnails in the Library View without having to read the actual photo file. This makes Picasa very fast because it doesn't need to read the actual photo file until you want to Edit, Copy, Print, or Export it.

Folders that you set to Scan Always should be in folders that you work with daily. You should not set up backups or archived photos to "Scan Always" because they are copies that should never be touched unless the original is accidentally deleted.

Setting a drive or folder to "Remove from Picasa" does not delete the folder or drive, it simply tells Picasa to ignore it and not show it to you in the library or folder collection.

**Set up your folders**

In the Picasa menu, go to **Tools** > **Folder Manager**

The "Folder List" on the left panel shows all the drives and folders on your computer.

To see sub-folders of the main drive or folder, click on the triangle to the left of the folder or drive to expand it and show the subfolders.

Set **My Pictures** and any other folders you want to see in Picasa to "**Scan Always**" (the blue circular arrow).

If you have photos in My Documents, also set My Documents to Scan Always.

If you have photos in a folder on the D drive, also set that folder to Scan Always.

Click the OK button when finished to verify the changes

For most normal photo collections, "**Scan Once**" (the green check mark) should never be used. However, you may want to scan photos on an external drive containing files that are not already on your computer. When set to "Scan Once," you can disconnect the external drive because when you later connect the drive, the photos will not automatically be scanned again because they have already been scanned once. * *

Example of the Folder Manager found under Tools in the Picasa menu

Notice that in the above example only My Pictures is set to Scan Always.

If you also have photos you want to see and edit in Picasa that are in other folders, you can also set those folders to Scan Always.

In general you should never set whole drives to Scan Always because that will result in images being shown that are part of programs, backups, and miscellaneous or temporary folders and locations.

If you want to see and edit photos on other drives, set the folders the photos are in on that drive to scan always, not the whole drive unless the whole drive contains only photos.

Small images less than 250 x 250 Pixels are not normally shown in Picasa. Picasa allows the user to hide photos so they are not shown and cannot be edited in Picasa.

Click the **View** menu and select **Small Pictures**

Click the **View** Menu and select **Hidden Pictures**

Picasa won't show any files or folders that have been marked hidden in Windows. To see if a folder is hidden, follow these steps:

Start Windows File Explorer, Go to the Tools > Options > View menu and set it to Show Hidden Files and Folders.

Navigate to the folder in question on your computer using Windows Explorer.

Right-click the folder or file.

Select **Properties**. The 'Hidden' checkbox must be deselected in order for Picasa to access content in the folder or file.

Repeat the above steps for every folder in the file path. For example, if your photo is stored at C:\Documents and Settings\fredflintstone\My Documents\My Pictures, you'll need to ensure that each of these folders (Documents and Settings, fredflintstone, My Documents, and My Pictures) is not hidden.

Note: If your filenames appear in light gray in Windows File Explorer, that means they're hidden.

Picasa has a feature that allows you to hide Photos or Folders so they are not seen in Picasa. The feature is not very useful because the photos and folders are still visible by any program outside of Picasa. Do this to make them visible again in Picasa.

**Unhide all Hidden photos in Picasa**

In the Picasa Search box on the top right, type the letter C. This selects all photos

On top left under albums category find the green starred album called "Search results for C"

On top menu click "Edit > Select All"

On top menu click "Picture > Unhide"

**Unhide Hidden folders in Picasa**

In Picasa3 switch to Flat Folder View: Go to the View Menu -> Folder View and select Flat Folder View.

In Flat Folder View in the left column, scroll down below the end of the Folders collection.

If there is a category called "Hidden Folders." Right-click on each hidden folder and select "Unhide folder"

If the Hidden Folder(s) are Password protected, enter the password.

If you don't know the password, reset it as follows:

Exit Picasa.

Confirm that you can view hidden files and folders in the Windows Explorer view menu.

Navigate to C:\Users\[USER]\AppData\Local\Google\Picasa2\db3 (where [USER] is your username).

Delete the catdata_info.pmp file in this db3 folder.

Restart Picasa.

If Picasa won't recognize photos you have stored on a network drive, please make sure that your connection is secure. If you continue to experience difficulties, you may want to consider storing your photos on your local hard drive instead.

Not everything will be automatically placed in your Folders collection. Folders containing items other than digital photos may be in the 'Other Stuff' collection. Likewise, your Folder List contains collections like 'Projects,' 'Downloaded Albums,' and 'Exported Pictures' which may also house your photos.

Picasa will not scan folders that have the following terms:

windows

winnt

temp

Program Files

Originals

Additionally, Picasa will not display folders or individual files that begin with a period. Consequently a folder like '**.picasaoriginals**' is filtered from displaying in Picasa. This is a hidden folder created by Picasa which stores edits made to photos. 

If a normally visible photo is in a filtered folder, move it to another folder to let Picasa access it.

Windows "Recycle Bin" is a special folder where Windows saves any files you delete in Windows File Explorer and many other programs that deal with files. When you delete a photo or folder in either File Explorer or Picasa, it goes into the Recycle Bin if it isn't too large to fit. How to recover files and photo folders from the Recycle Bin:

On your desktop, double click the Recycle Bin icon to launch it (or in the Start menu of Windows, type Recycle Bin and click the app)

In the Recycle Bin menu, click View > Details to show all details about the files in the bin.

You will see a column that says "Date Deleted". Click once or twice on the column title until the arrow points down. This sorts the items in the recycle bin so the most recent items are an top.

Look at the Names in the left column and find any folders or photos you deleted that you want to restore.

If you deleted folders, you will see only the folder name, but when restored, the photos inside will be restored, too.

Right-click on any folders and/or photos you want to restore, then select Restore. The folders and photos will again appear in Picasa.

You can view the photos as thumbnails to select which ones to restore. Go to the View Menu and select "Large icons" to see them as Thumbnails.

If the files are still lost, **do not do any operations on the last medium you had the pictures on! **See the warnings here** **How to Recover Deleted Photos and follow the steps to recover deleted files from your hard disk or memory card.

If the Picasa3 database is corrupt, it is possible that it will not show photos even if they are actually still in watched folders in the Folder Manager.

Follow the steps in this link to rebuild the Picasa database. **Method B is safer **because you can restore your original database if there is a problem:

How To Rebuild the Picasa Database

If your photos are still on your old computer and you forgot to move them to a new computer, that would be why you are unable to find missing photos. If you no longer have the old computer and never moved the photos to the new computer, and if you also have no backups anywhere (such as backups on external hard drives or at a "cloud" storage service), sadly, your photos are forever gone. Here's how to move photos and videos to a new computer:
