> Archived from the community-maintained Picasa Resources site:
> <https://sites.google.com/site/picasaresources/picasa/how-to-rebuild-database>
> Retrieved 2026-06-11 (text extracted with trafilatura).

The Picasa database is where Picasa keeps an index of all your pictures and videos along with information and thumbnails from all folders and pictures being scanned by Picasa. Rebuilding the database is the process of purging the database of damaged or incorrect information and then rebuilding the information. The rebuild process can take quite a while and depends a lot on the situation; plan on about an hour per 10,000 pictures for scanning pictures AND for doing face recognition.

**Read the advantages and disadvantages and choose the method of rebuilding the database below**. We recommend Method B because it is a "safer" method.

Rebuilding the Database is the process of purging the database of incorrect or damaged information and then rebuilding the information. The data to rebuild the database comes from the "MetaData" included in the photos and the hidden .picasa.ini files that Picasa writes to each folder containing pictures.

There are a number of cases where the Picasa database can become corrupt:

If Picasa unexpectedly closes or freezes, the database can become confused and not reflect the data of your files accurately.

The database thumbnails in the database refer to the wrong photos; when you view a photo, you see the wrong thumbnail.

Picasa continuously rescans your computer

**"CBlock"** database error is when the database becomes corrupted and causes Picasa to stop running. 

If your Picasa library is showing broken thumbnails such these, it's time to rebuild the database.

Some data is only saved in the Picasa database, so that data will be lost when rebuilding the database.

**Ignored faces: **any faces you have "ignored" will go back into the "unnamed" People album

**Picture order: **The order of pictures in both folders and albums is only saved in the Picasa database.

**Some movie edits:**

If you add or change the "date taken" or a "geotag" in a movie file, that information will only be stored in the Picasa database (not in metadata)

**Double face labels: **After Picasa finishes detecting faces, there will probably be some faces that will have a second rectangle for the same face (especially faces that were manually labeled in the past). This could be because the manual rectangle was a different size or placement or the face detection algorithm was improved. 

**In rare cases, any data that didn't make it inside the photo metadata or .picasa.ini files will be lost, such as: **

If the .ini files were deleted for any reason, the data stored in them will be lost (such as albums, cropping, etc.)

If an .ini file is set to read-only, Picasa won't add any data to it in the future

If a picture file is set to read-only, Picasa will no longer add the caption to the metadata. However, other metadata such as geotags and tag will be added.

**Advantages:**

Simple and endorsed by Google

**Disadvantages:**

Almost all faces will be grouped together but some may be labeled <Unknown Person> depending on how the People Names are stored.

If the setting at Tools > Options > Name Tags > "Store Name Tags in Photo" is enabled, only faces named after that setting was enabled will be retained.

If the People Names were only saved in the contacts.xml file in the database, the faces will be grouped together, but named <Unknown Person>. This is typically a small percentage of the Persons; it depends on what is stored in the .ini files and XMP Metadata in the photo.

See "**Limitations for Rebuilding**" above

**Database Rebuild Procedure:**

**Mac**

Open Finder

Browse to the following location: Your User Name > Library > Application Support > Google > Picasa3

Delete the db3 directory

**Windows**

Uninstall Picasa:

Click **Start** (lower left corner)

In the list of programs, find **Picasa 3 **folder** **and click it

Right-click Picasa3 and select **Uninstall**

In the confirmation window that opens, click **Uninstall**

**Important:** click **Yes** to remove the database

Once you've successfully uninstalled, you can download and reinstall Picasa. See How to get the final Picasa version

Start Picasa and set it up to scan My Pictures and My Documents folders (or the folders where you have pictures and videos).

Wait until Picasa completes scanning all folders and rescans the faces.

NOTE: Any time after Picasa starts scanning My Pictures and My Documents you can go to the Tools menu > Folder Manager and tell Picasa which folders to "Scan Always" and which to "Remove from Picasa" if you have custom picture folders.

**Advantages:**

*Method B is safer because you can go back to your previous database if you don't like the results.*

*It is not necessary to reinstall Picasa or tell it which folders to watch (in Folder Manager) if the folders were already defined.*

*Most face names will be restored due to keeping the Contacts.xml database file.*

**Disadvantages:**

*Requires familiarity with Windows operations and knowledge of "File Explorer" including ability to select, copy, paste, and delete files.*

*See "Limitations for Rebuilding" above*

**Database Rebuild Procedure:**

1) Close Picasa

2) Navigate to the Google application data folder on your computer by any of the two following methods:

Press the Windows key (sometimes labeled "Start") plus the R key to open the Run window

Open File Explorer

3) Paste the appropriate path below into the Run window or into the top of File Explorer of Windows 10:       **%LocalAppData%\Google\**

In **Windows 10**, the path is        C:\Users\<YourUserID>\AppData\Local\Google

In Windows XP, paste this: %userprofile%\Local Settings\Application Data\Google\

4) In the Google folder are two folders that have "Picasa" in the folder name: "**Picasa2**" and "**Picasa2Albums**

**Note that these folders have Picasa****2**** in their folder name even though the program is Picasa****3.**** **You will now copy those two folders, so that you have a backup. Here's how to copy the two folders:

Right-click on the Picasa2 folder and select Copy (or Ctrl+C), then right-click again into an empty space in the Google folder and select Paste (or Ctrl+P).

Pasting could take a long time (for example, 30 minutes or more) if you have a large collection of pictures. The copied folder will be named "**Picasa2 - Copy**"

Repeat the copy and paste process with the Picasa2Albums folder. The copied folder will be named "**Picasa2Albums - Copy**"

These two "- Copy" folders are your backup copy of the Picasa database.

5) In this step, you will be deleting some of Picasa's database files.

In the Google folder, double-click on the "**Picasa2**" folder to open it, then double-click the "**db3**" folder to view the database files. **Read carefully**: You will now delete **all **the database files in the **db3** folder **except** these three files:

scanlist.txt

thumbindex.db

thumbs_index.db

6) Start Picasa and wait. Picasa will begin scanning your computer for photos and video files. The rebuild process can take quite a while and depends a lot on the situation; plan on about an hour per 10,000 pictures for scanning pictures AND for doing Name Tags (face recognition).

Picasa will rescan faces, and it will see that they have been already tagged. Picasa will assign the correct name tag automatically.

You may see a couple of CBlock errors when Picasa starts; if so, just answer OK to each error.

     If Picasa fails to start, follow "**Picasa Fails to Start Using Alternate Method B**"** **directions below.

7) Evaluate the results long enough to be sure you are happy with the newly rebuilt database.

If the rebuilt database is not satisfactory, and you want to roll back to what you had before the rebuild, follow "**Roll Back to Previous Database**" directions below.

If the rebuilt database is satisfactory, you can delete the copy of the previous database, see "**Remove Previous Database**" directions below.

~End of Method B procedure~

* If you don't like the results after the rebuild, you can roll back to your old database by following these steps:*

1) Close Picasa

2) Navigate to the Google application data folder on your computer by any of the two following methods:

Press the Windows key (sometimes labeled "Start") plus the R key to open the Run window

Open File Explorer

3) Paste the appropriate path below into the Run window or into the top of File Explorer of Windows 10:       **%LocalAppData%\Google\**

In **Windows 10**, the path is        C:\Users\<YourUserID>\AppData\Local\Google

In Windows XP, paste this: %userprofile%\Local Settings\Application Data\Google\

*4) *There are four folders: **Picasa2**, **Picasa2Albums**, **Picasa2 - Copy**, and **Picasa2Albums - Copy**

The two "- Copy" folders are the backup of the "Old" database that was saved before the rebuild.

The Picasa2 and Picasa2Albums folders are the "New" database that was generated by the rebuild procedure.

If the two "- Copy" folders are not in the Google folder, it will not be possible to roll back to the old database.

If you want to keep a copy of the "New" database in case you want to try it again, rename the Picasa2 folder to Picasa2_new, and the Picasa2Albums folder to Picasa2Albums_new

If you are sure you don't want to keep the "New" database you just generated, delete the 2 folders Picasa2, and Picasa2Albums

Right-click on the "Picasa2 - Copy" folder and rename it to "Picasa2"

Right-click on the "Picasa2Albums - Copy" folder and rename it to "Picasa2Albums"

5) Start Picasa. It should start up immediately in the condition it was before the rebuild. There may be minor scanning if photos were added to your computer or changed since the rebuild.

~End of Roll Back Procedure~* *

*If you like the results after the rebuild, you can remove the unneeded "old" database by following these steps:*

1) Close Picasa

2) Navigate to the Google application data folder on your computer by any of the two following methods:

Press the Windows key (sometimes labeled "Start") plus the R key to open the Run window

Open File Explorer

3) Paste the appropriate path below into the Run window or into the top of File Explorer of Windows 10:       **%LocalAppData%\Google\**

In **Windows 10**, the path is        C:\Users\<YourUserID>\AppData\Local\Google

In Windows XP, paste this: %userprofile%\Local Settings\Application Data\Google\

4) There are four folders with "Picasa" in the folder name:** Picasa2** , **Picasa2Albums**, **Picasa2 - Copy**, and **Picasa2Albums - Copy**

Delete the two folders "Picasa2 - Copy" and "Picasa2Albums - Copy"

If the two "- Copy" folders are not there, the previous database was already removed.

~End of Remove Previous Database Procedure~

In "Alternate Method B" Step 5, Picasa could fail to start if there is a critical error in a remaining database file or if there are errors in the watched folders list. If Picasa fails to start or finds no photos, do the following:

1) Close Picasa

2) Navigate to the Google application data folder on your computer by any of the two following methods:

Press the Windows key (sometimes labeled "Start") plus the R key to open the Run window

Open File Explorer

3) Paste the appropriate path below into the Run window or into the top of File Explorer of Windows 10:       **%LocalAppData%\Google\**

In **Windows 10**, the path is        C:\Users\<YourUserID>\AppData\Local\Google

In Windows XP, paste this: %userprofile%\Local Settings\Application Data\Google\

4) In the Google folder, you had previously copied the folders named: "**Picasa2**" and "**Picasa2Albums**, so you should already have the two "- Copy" folders. If you don't have the two "- Copy" folders, then make a copy as described above. 

5) In the Google folder, double-click the "**Picasa2**" folder to open it, select the "**db3**" folder and delete it. 

6) Start Picasa. You will see a welcome page asking you if you want to scan everything or only My Pictures. Select only My Pictures for now (you can change this later). You will see another page asking you about Photo Viewer. Select 'Don't Use Picasa Photo Viewer (you can enable it later if you want it).

7) Picasa will start scanning your computer for photos and videos. The process may take an hour or more; about 2 hours per 10,000 pictures to rescan face tags.

8) To change which folders on your computer that Picasa should scan, go to Tools > Folder Manager. Set those you want to see displayed in Picasa to "Scan Always" then click "OK" so that any new folders will be scanned in the future. For folders you do not want scanned (not displayed in Picasa), choose the option "Remove from Picasa" then click OK. "Remove from Picasa" will not delete the photos from your computer.

~End of Picasa Fails to Start Procedure~

If, after rebuilding your database, Picasa scans your computer for hours and seems to be finished, yet later on it displays no photos and begins scanning all over again, here are some suggestion that may or may not fix that problem.

Rebuild the database again

Find the database folder on your computer (as above) and check folder permissions (also check permissions of your photos folders)

Open the **watchedfolders.txt** file in the database (at *C:\Users\**<YourUserID>**\AppData\Local\Google\Picasa2Albums*) and make sure the paths look correct. The watched folders text file lists folders you have set to be scanned (watched) under Tools > Folder Manager.

Open Folder Manager under Tools and look in the right side at the "Watched Folders" box. To better see the Folder Manager box, drag the edges of the window to make it larger.

Look for strange paths (such as a drive you don't have plugged in or no longer use). Click it and you should be taken to the "Folder List" on the left side. Try changing it to "Remove from Picasa."

Or from the Watched Folders box, click the strange path and click "Remove from Picasa" and it should remove itself from the "Watched Folders" box.

If you have a path showing "C:\Users\*<YourUserID>*\Downloads\Windows_Activation_Update\" (or any other strange path that no longer exists on your computer) and try to "Remove from Picasa," it may not remove itself from the "Watched Folders" box. Open the **watchedfolders.txt** file and manually delete that path, then save changes and close the text file. Open it again to see if was successfully removed. Unfortunately, if you change the setting in Folder Manager later, the strange path will appear again in the watchedfolders.txt file. 

Look at the Folders List (expand the drive where you store photos) and look for folders that aren't set up as you expected.

If sub-folders are set to "Scan Once" and you want it to "Scan Always," try toggling the main folder to "Remove from Picasa" then to "Scan Always" - you might have to change the sub-folders one at a time.

If a main folder is not expanded and you set it to "Scan Always," the sub-folders should all change to Scan Always; however, in some cases, this might not work and you will have to set the sub-folders manually.

Look in Tools > Experimental > "Choose database location" and make sure it says "Application Data\Google\Picasa2\" (this is the default location of the Picasa database)

Make sure your Windows user account has Administration access.

If you are using a corporate PC, there could be group policy rules.

Try to determine if a continuous backup service is interfering with Picasa's database somehow.

Be sure the drive which you are scanning is attached to the computer. If you are scanning an external drive and if the drive it not plugged in, Picasa will rescan it once it's attached to the computer.

Check if the database folder has Read/Write access:

Find the Google folder listed above (*C:\Users\**<YourUserID>**\AppData\Local\Google\)*

Right-click the Picasa2 folder and open Properties

In the General tab, make sure that under Attributes, "Read-only" and "Hidden" are not check marked

If check marked, uncheck and click "Apply"

Do the same for the Picasa2Albums folder

Even though you might be able to change the folder to not be "Read-only," when you go again to Properties, the "Read-only" box may be check-marked again. If that happens, you can search the internet for "windows properties read-only reverts" and see what help tips are available. One tip is the following:

Open File Explorer

Right click the drive where your files/folders are located and select "Properties"

Click the Security tab

Click the "Advanced" button

Click "Change permissions"

Select your user (such as "Authenticated Users") and click "Edit"

Select "This folder, subfolders and files" from the drop-down list

Check mark "Full Control" under Basic Permissions

Click OK

You may have to repeat this for other Principal users
