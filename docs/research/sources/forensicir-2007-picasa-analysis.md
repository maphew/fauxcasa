# Picasa — a forensic analysis

> Archived copy for reverse-engineering reference. Source: <https://forensicir.blogspot.com/2007/07/picasa.html>
> Retrieved 2026-06-11 (text extracted with trafilatura).

---

When my son was born I decided it would be a good time to organize all of the photos I'd taken to date.  I had experience with Picasa as a blogger, since picasaweb is associated with this blog, that is to say all of the images posted here have been indexed on picasaweb.


I went ahead and downloaded picasa.  Installation was quick and right after install, picasa wants to scan your entire hard drive for images so it can organize them.  Cool!  I was amazed at what was found on my computer in that I had forgotten about the presentation material I'd created a few years ago.


Anyways, I figured it would be worth it to run an analysis of picasa and share what I've found so far.



File Locations:

C:\Documents and Settings\USER\Local Settings\Application Data\Google\


Subdirectories:

\Picasa2

+-\db2

+-\runtime

+-\temp

+-\LifescapUpdater

+-\tmp

\Picasa2Albums



Registry Locations:

[HKEY_USERS\picasa\Software\Google]


[HKEY_USERS\picasa\Software\Google\Picasa]


[HKEY_USERS\picasa\Software\Google\Picasa\Picasa2]


[HKEY_USERS\picasa\Software\Google\Picasa\Picasa2\Filters]


[HKEY_USERS\picasa\Software\Google\Picasa\Picasa2\Preferences]

"DisableMediaDetector"=dword:00000000

"ytHLocal::langchange"=dword:00000000

"p1import"=dword:00000001

"p2import"=dword:00000001

"FixShortPathNames"=dword:00000000

"dbVersion"=dword:0000000f

"SupportBMP"=dword:00000001

"SupportPSD"=dword:00000001

"SupportTIF"=dword:00000001

"SupportPNG"=dword:00000000

"SupportGIF"=dword:00000000

"SupportMovies"=dword:00000001

"lastmypics"="\\serutciP yM\\stnemucoD yM\\ylfgoh\\sgnitteS dna stnemucoD\\:C"

"lastmydocs"="\\stnemucoD yM\\ylfgoh\\sgnitteS dna stnemucoD\\:C"

"lastdesktop"="\\potkseD\\ylfgoh\\sgnitteS dna stnemucoD\\:C"

"SaverUpgradedTo25"=dword:00000001

"Picasa Notifier"="rect(779 426 800 475)"

"LastNerdView"=dword:00000000

"LastCaptionButton"=dword:00000000

"Thumbscale"=dword:00000200

"mainwinismax"=dword:00000001

"mainwinpos"="rect(0 0 800 574)"

"LastAlbumSelected"="ca05c73419475ade037f8df528849c91"

"LastViewRoot"="flat"

"LastViewRoot2"="flat"

"WriteDirscannerCSV"=dword:00000000

"ytHLocal::lang"=dword:00000000

"datesort"=dword:00000000


[HKEY_USERS\picasa\Software\Google\Picasa\Picasa2\Preferences\Buttons]


[HKEY_USERS\picasa\Software\Google\Picasa\Picasa2\Preferences\Buttons\Exclude]


[HKEY_USERS\picasa\Software\Google\Picasa\Picasa2\Preferences\Buttons\UserConfig]


[HKEY_USERS\picasa\Software\Google\Picasa\Picasa2\Preferences\HotFolders]


[HKEY_USERS\picasa\Software\Google\Picasa\Picasa2\resvars]

"HLISTDIV"="0.216406"

"VLISTDIV"="0.1"

"HLISTOFFSET2"="240.000000"


[HKEY_USERS\picasa\Software\Google\Picasa\Picasa2\Runtime]

"AppPath"="C:\\Program Files\\Picasa2\\Picasa2.exe"

"StiDevice"=""

"resamplefile"=dword:00000001


[HKEY_USERS\picasa\Software\Google\Picasa\Picasa2\Update]

"UpdateNext"=dword:00000000


[HKEY_USERS\picasa\Software\Google\PicasaUpdate]

"PicasaUpdateID"="bd12e399a48a47e14d6562a1fe18c860"


Ok, there's a lot there in the registry, and I'll start with preferences.  Most of it's pretty straight forward, except for the paths that are reversed (what in the world is that all about?).  Of particular value, is the last album selected.   This value gets updated when picasa closes.  It appears to be an MD5sum - probably of the timedate and name of the album combined.  However, there's an easier way to determine what the value is.


in the db2 directory there are a number of files.  The important files for this are albumdata_token.pmp, albumdata_uid.pmp and albumdata_name.pmp


Here are the contents of the files:

albumdata_name.pmp - this is the name of the albums in picasa.  The first two are defaults and are not included in any of the other files.

Starred Photos

Screensaver

root

modified_for_tags

sam3

Sample Pictures

Sammy


albumdata_uid.pmp - This is where the hashes are.


b131d7e17dfdff73eb0340b4e9d3d6f3

8e92a45a6abed421488a5774ec3f4a4c

ca05c73419475ade037f8df528849c91

ec9771e026e3ce55c468354abcfce4ee

c332f1814ff6d4f21dbb41b41149544d


albumdata_token.pmp - Here's we see the uid applied to create a token for the albums.  Note that "star" and "screensaver" do not have uids.


]star

]screensaver

]album:b131d7e17dfdff73eb0340b4e9d3d6f3

]album:8e92a45a6abed421488a5774ec3f4a4c

]album:ca05c73419475ade037f8df528849c91

]album:ec9771e026e3ce55c468354abcfce4ee

]album:c332f1814ff6d4f21dbb41b41149544d


Now, if we look at the lastalbumselected value in the registry, we can pair it up to the hash since these files are all listed in the same order.  If you exclude star and screensaver you can see that the lastalbumselected for me was sam3.


You can even go one step further if you include albumdata_filename.pmp.  This file also matches up to the other files, except I forgot to mention one thing.  "root" is literally the root of the logical drive that picasa searched(in this case C:), so it is excluded from albumdata_filename.pmp.  This file contains the path to where the images are stored. 


Other files to pay attention to:

bigthumbs.db

thumbs2.db

thumbs.db

previews.db


These all follow the good old thumbs.db structure and contain thumbnails of all of the images at various resolutions, since picasa can send files directly to photo processing businesses.


One other thing that is of pretty vital importance in terms of proving that someone created an album and that the program didn't just index something.


In the Picasa2Albums directory you'll see a file for each of the album(s) created by the user under the folder using the DBID as its name.  Below are the contents of the album I created stored in  a file named c332f1814ff6d4f21dbb41b41149544d.pal. 

* I had to remove the leading bracket as blogger wanted to parse it *


'picasa2album>

'dbid>0164eaeacdd4046f5c1e44522fe44527

'albumid>c332f1814ff6d4f21dbb41b41149544d

'property name="uid" type="string" value="c332f1814ff6d4f21dbb41b41149544d">

'property name="category" type="num" value="0">

'property name="date" type="real64" value="39272.630035">

'property name="token" type="string" value="]album:c332f1814ff6d4f21dbb41b41149544d">

'property name="name" type="string" value="Sammy">

'files>

 'filename>[C]\sam3\sam1.jpg

 'filename>[C]\sam3\sam3.jpg

 'filename>[C]\sam3\sam2.jpg

 'filename>[C]\sam3\DSCF1890.JPG

'/files>

'/property>

'/picasa2album>


It's pretty self explanatory.  You can see the DBID (happens to match the updateid, so that's good), you see the albumid which as I've already shown, ties to Sammy. One oddity, is the Real64 date value.   Real64 is a float data type using 64 bits, but I've never run across it for a date stamp before.  If anyone wants to take a shot at converting that to a date, go ahead and let me know how you did it.




Other files of interest:

thumbindex.tid - This is an index of all images and paths to them.

wordhash.dat - Picasa indexes as much metadata as it can, so that you can search based off of it.  This file contains all parsed text from the images and their metadata.

repository.dat - This is a description of the database format used by picasa.

## Tuesday, July 10, 2007

Subscribe to:
Post Comments (Atom)

## 0 comments:

Post a Comment
