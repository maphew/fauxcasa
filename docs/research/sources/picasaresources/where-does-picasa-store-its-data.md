> Archived from the community-maintained Picasa Resources site:
> <https://sites.google.com/site/picasaresources/picasa/where-does-picasa-store-its-data>
> Retrieved 2026-06-11 (text extracted with trafilatura).

Picasa stores data about pictures in 3 locations: the photo files themselves, inside .picasa.ini files, and in the Picasa database.

**In the photo files **themselves: if there exists standards how to put the data in the photo file (.jpg), Picasa typically will put it there. This way the data cannot get lost and you can reuse the data in other applications that support standards as well. Examples:

Captions

Keywords

Geotags

Name tags (if you enable this in "Tools"/"Options..."/"Name tags"

Date taken

**In .picasa.ini files**: if there isn't a standardized way to store the data in the .jpg, Picasa will put the information in a hidden .picasa.ini file in the directory where the photo file is located. Examples of this data:

All edits you applied to images (as long as you don't "Save" the edits)

Albums in which the photo was added

**In the Picasa database**: the Picasa database in theory is a redundant copy of all the information listed above, but is stored in files that are optimized to enable fast searching. The Picasa database is created in the "user profile" so every user that uses the computer has its own copy.

Based on the photo files and the .picasa.ini files you can recreate the database. However, there are some exceptions to this due to some bugs or incomplete features in Picasa. See Limitations for Rebuilding
