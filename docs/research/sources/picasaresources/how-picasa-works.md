> Archived from the community-maintained Picasa Resources site:
> <https://sites.google.com/site/picasaresources/picasa/how-picasa-works>
> Retrieved 2026-06-11 (text extracted with trafilatura).

Picasa is fundamentally a Photo Manager with some basic editing features.

Picasa watches the folders you tell it to scan and builds an index of the photo locations and thumbnails of the Photos so it can show you the photos instantly.

This index and thumbnail collection is called the database and it is responsible for Picasa's ability to manage very large libraries while remaining responsive.

What Picasa does:

Picasa keeps a "Database" in the user data area. This database consists of a number of cross referenced lists and indexes in binary form of each photo it is "Watching" which contains all the information about the photo including its file name, folder path, captions, editing information, a thumbnail, and a list of all "words" that apply to that photo. All of this information is stored and cross referenced in machine readable form in the database files.

Most Photo file formats like .Jpg, Tif, etc. contain a data area called the MetaData where the camera stores the date the photo was taken, the focal length of the lens, the photo size, and much other data. Picasa stores Face tags and other data about the photo in the metadata in an area called the XMP data. This data is in addition to the Picture itself.

Picasa also maintains a hidden file in each folder named ".picasa.ini". This file is a redundant copy of all the information and edits for each photo in that folder. Picasa maintains this file as a backup to the database.

If the Database is corrupted it can be rebuilt by compiling all the Photo MetaData from each photo along with the data in the .ini files in each folder into a new database.

Picasa has a list of Folders in it's Database that have been set to "Scan Always" in the Folder Manager. For each of these folders it keeps a list of the photos in that folder. For each of these photos it keeps all the information about the photo in the various binary database files. While Picasa is running it is continuously scanning through all the watched folders, watching for added or deleted photos. When you add, copy, or move a photo into a folder being "watched" by Picasa, the next time it scans that folder watching for changes it will find the new Photo. When Picasa finds a new photo in a watched folder, it examines it and adds all the information about that photo to the various Database lists and data files. Since Picasa now knows that photo is in that folder, it can show it to you in it's folder list and library.

Compacting: When you exit Picasa3, you may see a message about compacting. Let it run until it finishes. Compacting does not affect or harm your folders or photos in any way. Compacting just affects the Picasa database where it stores all the information it knows about your photos. As you work on your photos and edit or manage them, the database develops sections of obsolete data that is no longer valid. The amount of this obsolete information builds up making your database bigger and less efficient. At some point Picasa decides to "Compact" the database by deleting this obsolete data and then moving new data into the holes making the database more compact since it then has no wasted space.

*Some important things to note from the above:*

You tell Picasa which folders it is to "Watch" by setting them to "Scan Always" in the Tools menu -> Folder Manager.

All folders and drives that you don't want to see in Picasa are set to "Remove From Picasa" in the Folder Manager. This doesn't delete the folders or the photos inside, it simply tells Picasa to ignore them and act like they don't exist.

Picasa doesn't change the locations of your photos or copy them or anything else, they are still in the folders you originally put them in.

Picasa Database locations:

The Picasa database is in the user data area at this Path (paste the appropriate path below into Windows Explorer):

In Windows XP: %userprofile%\Local Settings\Application Data\Google\

In Windows Vista / Windows 7 and later: %LocalAppData%\Google\

In the Google Folder in the above path, the database is contained in two subfolders, "**Picasa2\**" and "**Picasa2Albums**"

The Picasa2 subfolder contains the bulk of the Database in it's "**db3**" subfolder.

The Picasa2 subfolder also contains a contacts subfolder which contains the **contacts.xml** file where all the face names are referenced.

The Picasa2Albums folder contains two files that define folders for Picasa:

The **WatchedFolders.txt** file defines which folders are set to Scan Always in the Picasa Folder Manager.

The **FRExcludeFolders.txt** file defines which folders have Face Recognition turned off.

All the database files are in a specific format and when edited the database may become corrupt.

Other Picasa associated file locations:

Each Folder that contains photos will also contain the hidden "**.picasa.ini**" file that contains the edit and other data about each photo.

Each Folder that contains photos that have had their edits "Saved" will contain a hidden subfolder called "**.picasaoriginals**" The .picasaoriginals subfolder contains the original photo before edits were applied for any photos that had their edits saved into the photo.

Picasa reads the MetaData inside each Photo that supports MetaData and adds that data to it's database.
