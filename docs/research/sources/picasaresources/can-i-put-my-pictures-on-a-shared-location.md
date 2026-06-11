> Archived from the community-maintained Picasa Resources site:
> <https://sites.google.com/site/picasaresources/picasa/can-i-put-my-pictures-on-a-shared-location>
> Retrieved 2026-06-11 (text extracted with trafilatura).

You can sharing and access pictures from multiple computers but some features behave slightly different. On some NAS devices, there seems to be some additional problems.

Most data is stored in the .jpg files or the .picasa.ini files and all this data will be read and shared by all instances of Picasa scanning those same folders so everyone will see the edits.

There are some things that are only saved locally on the computer in the "Picasa database" instead of alongside the pictures and they won't be "reused" on each computer. They are listed in the "Limitations on rebuilding" section of the following article: Rebuild the Picasa database

One user reported that all information in the .picasa.ini files was only seen on the computer on which edits were made. Crops, albums, etc., were not seen on the other computer.

On some NAS devices there seem to be some problems. The only way to know for your specific situation is to test it out. These things have been reported before:

Some NAS devices have a "problem" with filenames that begin with a period. Like the ".picasa.ini" file

Some NAS devices don't report properly to Windows/Picasa that a file was changed. In this case, Picasa won't notice in real-time that a change was made on another computer. Normally the change should be "seen" once you restart Picasa.
