# How to access the Picasa (desktop) database?

> Archived copy for reverse-engineering reference. Source: <https://stackoverflow.com/questions/1467004/how-to-access-the-picasa-desktop-database/8482061>
> Retrieved 2026-06-11 (text extracted with trafilatura). Stack Exchange content is CC BY-SA.

---

Is there any way to read the Picasa database?

What is the format of the Picasa database?

Are there any APIs to access the database?

In Picasa 3 at least, the internal database is stored in a set of `.pmp` files that sits alongside the `.db` files, in one of the standard locations for Picasa's application data. On the Mac for instance, it is under `$HOME/Library/Application Support/Google/Picasa3/db3`

Rather confusingly, the `.db` files don't contain the database, they are just containers that hold thumbnail previews for various groups of images. They are in the standard Windows `thumbs.db` format, more information from this answer.

The `.pmp` files contain the database, and are in a non-standard format. There is a cluster of files per table, with one file per field. The filenames for a given table share the same prefix. For example, the data in the `catdata` table comes from this set of files:

```
$ ls -1 catdata_*
catdata_0
catdata_catpri.pmp
catdata_name.pmp
catdata_state.pmp
```
which has three fields, `catpri`, `name` and `state`. I've written up some partial notes in a blog on the format of these files as of Picasa 3.9.0.522, as well as a small java program to dump out as much of the data as I've been able to understand.

You can try to read the Picasa database using the exportpicasa utility (http://sourceforge.net/projects/exportpicasa/). It's beta and feedbacks are welcome.

To me it looks like there is no 'database' per se.

There is a file that lists the folders picasa 'watches', for vista it is in

```
C:\Users\<myaccount>\AppData\Local\Google\Picasa2Albums\ 
```
and for XP in

```
C:\Documents and Settings\<myaccount>\Local Settings\application data\google\Picasa2Albums\
```
Inside the watched folders there are `.picasa.ini` and `picasa.ini` files that store some data.

All of these files are human readable, so they should be parseable pretty easily.

Any modern image library should be able to parse the IPTC data (in python try "from PIL import IptcImagePlugin")

In the database folder (on Windows 7: C:\Users\User\AppData\Local\Google\Picasa2\db3), there are some *pmp* files representing the following tables:

Each pmp file contains all the data of one column of the table. The filename name follow the schema *table*_*column*.pmp. The file itself is in a binary format.

Then, the filenames (for the pictures, or the path of the folders) are inside the file *thumbindex.db*, which is binary and different from the pmp files.

Detailed explanation of the 2 binary formats: How to Read Picasa 3.9 Database and extract faces data
