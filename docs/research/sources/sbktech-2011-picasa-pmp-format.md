# The Picasa .pmp format

> Archived copy for reverse-engineering reference. Source: <https://sbktech.blogspot.com/2011/12/picasa-pmp-format.html>
> Retrieved 2026-06-11 (text extracted with trafilatura).

---

I recently needed to dig into Picasa's internal databases to get some information that it appeared to store only there, and not finding the answer on the interwebs, here are my notes about their format. Please do let me know if you have more information about this file format.

The notes are for the Mac OS, Picasa version 3.9.0.522.

The database files are found under

`$HOME/Library/Application Support/Google/Picasa3/db3`

on the Macs, and there are equivalent locations on other platforms. Under here are a set of files with a `.pmp` suffix, which are the database files.

[BTW: The files with the `.db` suffix just hold thumbnails of various groups  of images. They are in the standard windows `thumbs.db` format, and here's a link that has more useful information about this format.]

Each `.pmp` file represents a field in a table, and the table is identified by a common prefix as follows:

$ ls -1 catdata_* catdata_0 catdata_catpri.pmp catdata_name.pmp catdata_state.pmp

The file with the `_0` suffix is a marker file to identify the table, and each `.pmp` file sharing that prefix is a field for that table. For instance, `catdata_state.pmp` contains records for the field `state` in the table `catdata`, and so forth.

All files start with the four magic bytes: `0xcd 0xcc 0xcc 0x3f`

The marker files (ie, files that end in `_0`) only contain the magic bytes.

The pmp file is in little-endian format rather than the usual network byte/big-endian format.

There are several areas where I just see constants -- I don't know the purpose of these and I'll list them out. Please note: all values are presented in little-endian format, so if you hex-dump a file, you should see the bytes reversed.

Header

4bytes: magic: 0x3fcccccd

2bytes: field-type: unsigned short.

2bytes: 0x1332 -- constant.

4bytes: 0x00000002 -- constant.

2bytes: field-type: unsigned short -- identical with field-type above.

2bytes: 0x1332 -- constant.

4bytes: number-of-entries: unsigned int.


Following the header are "number-of-entries" records, whose format depends on the field-type. The field-type values are:

0x0: null-terminated strings. I haven't tested how (if at all) it can store unicode.

0x1: unsigned integers, 4 bytes.

0x2: dates, 8 bytes as a double. The date is represented in Microsoft's Variant Time format. The 8 bytes are a double, and the value is the number of days from midnight Dec 30, 1899. Fractional values are fractions of a day, so for instance, 3.25 represents 6:00 A.M. on January 2, 1900. While negative values are legitimate in the Microsoft format and indicates days prior to Dec 30, 1899, the Picasa user interface currently prevents dates older than Dec 31, 1903 from being used.

0x3: byte field, 1 unsigned byte.

0x4: unsigned long, 8bytes.

0x5: unsigned short, 2bytes.

0x6: null-terminated string. (possibly csv strings?)

0x7: unsigned int, 4 bytes.

The entities are indexed by their record number in each file. Ie, fetching the 7273'rd record in all files named `imagedata_*pmp` gives information about the fields for entity #7273 in the `imagedata` table.

You might expect every "field file" for a given table to contain the same number of records, but this is not always the case. I expect the underlying library returns the equivalent of undefined when fetching fields for a record beyond the "end" of any given field file.

Finally, a small java program to dump out whatever information I've gathered thus far. Compile, and run against a set of .pmp files.

Here is a sample run.

$ javac -g -d . Read.java $ java Read "$HOME/Library/Application Support/Google/Picasa3/db3/catdata_name.pmp" /Users/kbs/Library/Application Support/Google/Picasa3/db3/catdata_name.pmp:type=0 nentries: 10 [0] Labels [1] Projects (internal) [2] Folders on Disk [3] iPhoto Library [4] Web Albums [5] Web Drive [6] Exported Pictures [7] Other Stuff [8] Hidden Folders [9] People

And here's the code.

```
import java.io.*;
import java.util.*;
public class Read
{
    public static void main(String args[])
        throws Exception
    {
        for (int i=0;i <args.length; i++) {
            doit(args[i]);
        }
    }
    private final static void doit(String p)
        throws Exception
    {
        DataInputStream din = new DataInputStream
            (new BufferedInputStream
             (new FileInputStream(p)));
        dump(din, p);
        din.close();
    }
    private final static void dump(DataInputStream din, String path)
        throws Exception
    {
        // header
        long magic = readUnsignedInt(din);
        if (magic != 0x3fcccccd) {
            throw new IOException("Failed magic1 "+Long.toString(magic,16));
        }
        int type = readUnsignedShort(din);
        System.out.println(path+":type="+Integer.toString(type, 16));
        if ((magic=readUnsignedShort(din)) != 0x1332) {
            throw new IOException("Failed magic2 "+Long.toString(magic,16));
        }
        if ((magic=readUnsignedInt(din)) != 0x2) {
            throw new IOException("Failed magic3 "+Long.toString(magic,16));
        }
        if ((magic=readUnsignedShort(din)) != type) {
            throw new IOException("Failed repeat type "+
                                  Long.toString(magic,16));
        }
        if ((magic=readUnsignedShort(din)) != 0x1332) {
            throw new IOException("Failed magic4 "+Long.toString(magic,16));
        }
        long v = readUnsignedInt(din);
        System.out.println("nentries: "+v);
        // records.
        if (type == 0) {
            dumpStringField(din,v);
        }
        else if (type == 0x1) {
            dump4byteField(din,v);
        }
        else if (type == 0x2) {
            dumpDateField(din,v);
        }
        else if (type == 0x3) {
            dumpByteField(din, v);
        }
        else if (type == 0x4) {
            dump8byteField(din, v);
        }
        else if (type == 0x5) {
            dump2byteField(din,v);
        }
        else if (type == 0x6) {
            dumpStringField(din,v);
        }
        else if (type == 0x7) {
            dump4byteField(din,v);
        }
        else {
            throw new IOException("Unknown type: "+Integer.toString(type,16));
        }
    }
    private final static void dumpStringField(DataInputStream din, long ne)
        throws IOException
    {
        for (long i=0; i<ne; i++) {
            String v = getString(din);
            System.out.println("["+i+"] "+v);
        }
    }
    private final static void dumpByteField(DataInputStream din, long ne)
        throws IOException
    {
        for (long i=0; i<ne; i++) {
            int v = din.readUnsignedByte();
            System.out.println("["+i+"] "+v);
        }
    }
    private final static void dump2byteField(DataInputStream din, long ne)
        throws IOException
    {
        for (long idx=0; idx<ne; idx++) {
            int v = readUnsignedShort(din);
            System.out.println("["+idx+"] "+v);
        }
    }
    private final static void dump4byteField(DataInputStream din, long ne)
        throws IOException
    {
        for (long idx=0; idx<ne; idx++) {
            long v = readUnsignedInt(din);
            System.out.println("["+idx+"] "+v);
        }
    }
 
    private final static void dump8byteField(DataInputStream din, long ne)
        throws IOException
    {
        int[] bytes = new int[8];
        for (long idx=0;idx<ne; idx++) {
            for (int i=0; i<8; i++) {
                bytes[i] = din.readUnsignedByte();
            }
            System.out.print("["+idx+"] ");
            for (int i=7; i>=0; i--) {
                String x = Integer.toString(bytes[i],16);
                if (x.length() == 1) {
                    System.out.print("0");
                }
                System.out.print(x);
            }
            System.out.println();
        }
    }
    private final static void dumpDateField(DataInputStream din, long ne)
        throws IOException
    {
        int[] bytes = new int[8];
        for (long idx=0;idx<ne; idx++) {
            long ld = 0;
            for (int i=0; i<8; i++) {
                bytes[i] = din.readUnsignedByte();
                long tmp = bytes[i];
                tmp <<= (8*i);
                ld += tmp;
            }
            System.out.print("["+idx+"] ");
            for (int i=7; i>=0; i--) {
                String x = Integer.toString(bytes[i],16);
                if (x.length() == 1) {
                    //System.out.print("0");
                }
                //System.out.print(x);
            }
            //System.out.print(" ");
            double d = Double.longBitsToDouble(ld);
            //System.out.print(d);
            //System.out.print(" ");
            // days past unix epoch.
            d -= 25569d;
            long ut = Math.round(d*86400l*1000l);
            System.out.println(new Date(ut));
        }
    }
    private final static String getString(DataInputStream din)
        throws IOException
    {
        StringBuffer sb = new StringBuffer();
        int c;
        while((c = din.read()) != 0) {
            sb.append((char)c);
        }
        return sb.toString();
    }
    private final static int readUnsignedShort(DataInputStream din)
        throws IOException
    {
        int ch1 = din.read();
        int ch2 = din.read();
        if ((ch1 | ch2) < 0)
            throw new EOFException();
        return ((ch2<<8) + ch1<<0);
    }
    private final static long readUnsignedInt(DataInputStream din)
        throws IOException
    {
        int ch1 = din.read();
        int ch2 = din.read();
        int ch3 = din.read();
        int ch4 = din.read();
        if ((ch1 | ch2 | ch3 | ch4) < 0)
            throw new EOFException();
        long ret = 
            (((long)ch4)<<24) +
            (((long)ch3)<<16) +
            (((long)ch2)<<8) +
            (((long)ch1)<<0);
        return ret;
    }
}
```
hello






thanks for your great work!

i have adopted your code to use it in order to get a complete record: ReadPicasaDBrow.java (sorry, could not post it to the comment as it would exceed the allowed characters limit).

use it as follows:

$ java ReadPicasaDBrow PathToPicasaDBdir TableName ZeroBasedRecordNumber

e.g. like

$ java ReadPicasaDBrow "$HOME/Library/Application Support/Google/Picasa3/db3" catdata 5

regards,

martin.

Unknown said...

June 1, 2012 at 5:05 AM

Awesome work! Do you have any idea how these records map to an actual filename? For instance, if I look at imagedata_tags.pmp and see a line like this:




[27725] vacation,friends

do you know how I'd figure out what actual file path the 27725 record is pointing to with those tags?

It's really late so I may be overlooking something simple but I have been poking around a bit and haven't found it.

Jonathan Dean said...

November 15, 2012 at 10:40 PM

Great explanation! Thanks to your article and some additional reading, I figured out how to link each image from the pmp files to its filename (information in thumbindex.db) and the corresponding face album (if a face is present in the image).

I reused some of your code for my program: http://skisoo.com/blog/en/2013/how-to-read-picasa-3-9-database-and-extract-faces/

skisoo said...

May 13, 2013 at 1:54 PM

Hi - I'm wondering if you can help. I read elsewhere that I could access the catname.pmp file to remove categories from the drop down list that I no longer needed. I copied the original elsewhere for safety and opened the .pmp with notepad. The changes I wanted were not reflected. I then pasted the original back into app data folder (windows 7) but now it doesn't know what to open it with? When I right click/properties/ it says it opens with notepad. I changed that to open with picasa.exe but all of my categories in picasa now read as 'other stuff'?

How do I get picasa to read this table properly again? Any help is appreciated!

Within A Quarter Inch said...

October 3, 2013 at 7:54 AM

In response to Jonathan Dean's question (it's an old question, but on the off chance he's still interested, or if someone else has the same question):





See the great page skisoo wrote and gave the URL for, in a comment above.

Excerpt from skisoo's page ( http://skisoo.com/blog/en/2013/how-to-read-picasa-3-9-database-and-extract-faces/ ):

"This information is held in the file thumbindex.db. This file contains the whole list of folders and files indexed inside the Picasa database. the line x in thumbindex.db file will correspond to the same image as the line x in the table imagedata."

(The only slight simplification there is that the file first lists, on a few separate lines, the directories.)

See also http://projects.mindtunnel.com/picasa3meta/docs/picasa3meta.thumbindex.ThumbIndex-class.html (which skisoo's page directed me to).

Jacob Cappell said...

October 16, 2013 at 12:26 PM

Thank you for this excellent work!






Do you know which file(s) contain the Folder Manager paths (places to watch for image changes)?

I've moved all my pictures to a larger volume and need to repoint Picasa to the new location, without rescanning the whole darn thing (it's several hundred gigabytes).

I'm happy to use change D:\Pictures to X:\Pictures in a hexeditor, I just haven't been able to locate the right places yet.

I thought had it with `repository.dat` but that's not it, or at least not complete.

thanks!

maphew said...

November 23, 2014 at 1:50 PM

Play the laughter, Pause the memories, Stop the pain, Rewind the happiness.

Your article is very interesting please visit my site too gofastek.com. God Bless :)

Unknown said...

September 25, 2016 at 5:57 PM

Do you know that there are best Windproof pipe lighters throughout the internet? Of course,It is hard to light the tobacco inside the channel with ordinary lighters. Nonetheless, the channel lighters have become the most significant and commendable instrument for lighting pipe.


maxwell said...

May 28, 2020 at 12:20 AM

Thanks for sharing this wonderful information with us . It is just fantastic.






AngularJS training in chennai | AngularJS training in anna nagar | AngularJS training in omr | AngularJS training in porur | AngularJS training in tambaram | AngularJS training in velachery

latchu kannan said...

June 11, 2020 at 10:47 PM

Great content & Thanks for sharing with oflox India, Website Design Company In Dehradun

Sunder said...

September 30, 2020 at 11:14 PM

Aivvu chuyên vé máy bay, tham khảo










vé máy bay đi Mỹ giá bao nhiêu

săn vé tết 2021

giá vé máy bay đi toronto Canada

vé máy bay Việt Nam đi Pháp

vé máy bay đi Anh

mua vé máy bay giá rẻ ở đâu

combo đà nẵng 4 ngày 3 đêm 2021

combo nha trang 3 ngày 2 đêm

Vietnam Airline said...

December 14, 2020 at 8:19 PM

Aivvu chuyên vé máy bay, tham khảo










vé máy bay đi Mỹ giá bao nhiêu

săn vé tết 2021

giá vé máy bay đi toronto Canada

vé máy bay Việt Nam đi Pháp

vé máy bay đi Anh

mua vé máy bay giá rẻ ở đâu

combo đà nẵng 4 ngày 3 đêm 2021

combo nha trang 3 ngày 2 đêm

Vietnam Airline said...

December 14, 2020 at 8:21 PM

Really nice and interesting post. I was looking for this kind of information and enjoyed reading this one. Keep posting. Thanks for sharing.








tally training in chennai

hadoop training in chennai

sap training in chennai

oracle training in chennai

angular js training in chennai

jhansi said...

December 27, 2020 at 11:35 PM

I was glad to reveal this extraordinary site. I need to thank you for your time because of this awesome read!! I unquestionably appreciated all of it and I have you bookmarked to see new data on your blog.

best interiors

Unknown said...

July 9, 2021 at 8:55 PM

"Wonderful contribution!" I'm blown away by the information you've shared. Thank you so much for everything. Continue to write your blog.

Best Interior Designers

Unknown said...

August 4, 2021 at 5:21 AM

I was glad to reveal this extraordinary site. I need to thank you for your time because of this awesome read!!

Online School Management Software In India

rohit said...

December 20, 2021 at 2:23 AM

Thanks For Sharing such a wonderful article the way you presented is really amazing..Embedded Systems Course in Hyderabad

Sunny said...

August 22, 2022 at 10:51 PM

Thanks For Sharing such a wonderful article the way you presented is really amazing.



Here is sharing some SnapLogic concepts may be its helpful to you.

SnapLogic training

vinayak k said...

June 12, 2023 at 6:42 AM

Best Alternative to Laxmikanth Polity

clat syllabus

sociology optional syllabus

sociology optional coaching online

clat coaching in hyderabad

careergearup said...

September 14, 2023 at 9:28 AM

sociology optional coaching in hyderabad


Why Sociology is the best

Top Law Entrance Exams in India

ias coaching in hyderabad

careergearup said...

September 14, 2023 at 9:29 AM

digital marketing course in hyderabad

digital marketing course in telugu

wordpress training in hyderabad

video editing course in hyderabad

seo training in hyderabad

digitalbadi said...

September 14, 2023 at 11:59 PM

Nice blog

ca coaching in hyderabad

ca foundation course

ca final course

ca intermediate cousre

cma coaching in hyderabad

cma foundation course

cma final course

cma intermediate course

ajacommerce said...

October 4, 2023 at 1:39 AM

"Great article, felt good after reading, worth it.

i would like to read more from you.

keep posting more.

also follow Propmtengineeringcourseinhyderabad"

rudrasa said...

March 29, 2024 at 10:44 PM

"Great article, felt good after reading, worth it.

i would like to read more from you.

keep posting more.

also follow Mern Stack course in hyderabad"

lavanya said...

April 2, 2024 at 2:26 AM

nice work "Top Digital Marketing Agency In Hyderabad

"

lavanya said...

April 7, 2024 at 11:48 PM

Glory Avenues Private Limited is a Hyderabad-based real estate company specializing in premium residential plot ventures, committed to transparency, trust, and customer satisfaction.

Glory Avenues Private Limited

Anonymous said...

April 8, 2025 at 4:00 AM

Thanks For Sharing such a wonderful article the way you presented is really amazing.


Here is sharing some concepts may be its helpful to you.

Shrine Publishers Reviewed Journals

shrinepublishers said...

September 5, 2025 at 12:52 AM

Loved the clarity in this post. From concept to completion, a professional firm makes the journey stress-free. At Shruti Sodhi Interior Designs, we deliver quality as one of the trusted interior design firms in Delhi.

Shruti Sodhi Interior Designs said...

December 19, 2025 at 4:50 AM
