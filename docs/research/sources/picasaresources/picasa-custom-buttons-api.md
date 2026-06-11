> Archived from the community-maintained Picasa Resources site:
> <https://sites.google.com/site/picasaresources/picasa/picasa-custom-buttons-api>
> Retrieved 2026-06-11 (text extracted with trafilatura).

The Picasa user interface (UI) includes custom buttons which can open image files in local applications as well as upload the selected image files to the web using the Picasa Web Uploader.

**Buttons in Picasa**

As a developer, you can easily add your own button to the Picasa UI using the picasa: pluggable protocol. This is particularly useful for third-party developers who wish to integrate with Picasa, as it increases the presence of a service brand in the UI and enables end users to export images from Picasa into another application or service with one click.

To creating a custom button, you need to:

**Audience**

This documentation is intended for programmers who want to add custom buttons to the Picasa software user interface. You should be familiar with Picasa, XML, Adobe® Photoshop®, the Windows registry and executable files. This documentation provides descriptions of the necessary components of this technology and a reference of an XML schema that allows you to control how your buttons will function.

This API makes use of several file types:

**PBF** - See below.

**Photoshop file (PSD)** - The graphic for the Picasa button. It can be no larger than 40 pixels wide by 25 pixels tall, should be drawn in RGB/8-bits per channel mode at 72 dpi, and drawn with a transparent background

**Picasa Button Zipfile (PBZ) file** - Archive that contains the the PBF and PSD files

Since PBZ files are essentially your redistributable packages, you can use more recognizable names than the PBF and PSD files they contain (see Naming Your Files). However, be sure to name your PBZ files with some measure of uniqueness, so as to avoid conflicts with an existing PBZ of a similar name. It is a good rule of thumb to use your company name or website name in your PBZ filenames to provide some namespace differentiation. For example, a filename like acme-inc-photo-uploader.pbz is unlikely to have a name collision, and it makes the button's origin obvious. In contrast, a filename like uploader.pbz is far too generic and likely to cause confusion. (If in doubt, use a GUID as the filename if human readability is not important.)

In addition, using a PBZ file allows the distribution of multiple buttons in a single file. Internally, this is exactly what Picasa does. All of the default buttons you see when you first install Picasa are contained in a single PBZ file.

Each button you add is defined by an individual PBF file. There are four elements of each button:

**Icon** - (*Optional*) This the graphic that appears on the button. If you do not specify an icon, Picasa uses a default icon.

**Label** - This is the text that appears on the button directly below the icon.

**Tooltip** - This is the text that displays when the mouse pointer hovers over the button. The tooltip text also appears as the button’s description in Picasa’s button configuration dialog box.

**Verb** - Each button needs a verb. A number of built-in, generic verbs are supported. Through verbs, you can get images out of Picasa. For example, you can use the trayexec verb to process all of the pictures in the Picasa tray with an arbitrary executable. Learn about supported verbs.

You can specify localization of the label and tooltip text for the languages supported by Picasa. All localized text must be present in the button configuration file.

You must name your PBF using a Globally Unique Identifier (GUID). A GUID ensures that no two buttons will be confused for one another, which can occur with common button names such as “Upload Button”. GUIDs are created using algorithms that use unique aspects of a machine’s hardware and software environment, such as the network interface MAC address and the current date and time. Learn more about GUIDs. Picasa uses the Windows registry format for GUIDs. A typical GUID in this registry format looks like this:

{3acc6bb4-ffd9-11da-95f3-00e08161165f}

Note that for Picasa, the curly braces are included in the GUID.

Creating a GUID for your button is straightforward. Many tools exist for creating GUIDs in the required format. If you are using a Windows development environment like Visual Studio, the executable guidgen.exe is automatically installed for you. guidgen.exe will create a GUID for you at the click of a button. If you do not have a development environment installed, web-based solutions exist, such as guidgen.com. (Perform a web search for the term “guidgen” if you need help finding a guild generator). Once you have obtained a GUID for your button, save it to a text file for later use, as you will need to copy and paste it a few times.

For example, the PBF file that describes the button with the GUID above would be named:

{3acc6bb4-ffd9-11da-95f3-00e08161165f}.pbf

The Photoshop (PSD) file that contains the icon for the button should be named similarly:

{3acc6bb4-ffd9-11da-95f3-00e08161165f}.psd

Finally, use the GUID inside the PBF file’s XML code to identify the button and to refer to the PSD file.

The basic XML structure of a PBF is as follows:

<?xml version="1.0" encoding="utf-8" ?>

 <buttons format="1" version="*version_number*">

   <button id="*buttonid*" type="dynamic">

     <icon name="*PSDfile/layername*" src="pbz"/>

     <label>*Button Label*</label>

     <tooltip>*Tooltip text goes here.*</tooltip>

<action verb="open"|"trayexec"|"hybrid">

       <param name="*name*" value="*value*"/>

</action>

</button>

</buttons>

You can use the above snippet as a starting point, editing it as needed. See Verbs and Parameters below.

There are three verbs that you can use in the <action> tag: trayexec, hybrid and open.

trayexec

<action verb="trayexec">

      <param name="exe_name" value="*executable*"/>

      <param name="export" value="1"/>  *(optional)*

      <param name="foreach" value="1"/> *(optional)*

</action>

This action launches an executable, passing the filenames of the images in the Picasa tray as command line parameters. For example, you can use it to open all of the files in the Picasa tray with another program like Photoshop. Many options exist for this action, all exposed via the <param> tags.

<action verb="hybrid">

    <param name="url" value="*hybrid_uploader_url*"/>

</action>

This action launches the Picasa Web Uploader, using the specified webpage for its content. The Web Uploader is the best way to integrate a web service with Picasa. For example, the BlogThis! feature uses the Web Uploader to export images from Picasa to a user’s blog via Blogger.

The name attribute must be “url” and the value attribute must specify the URL of your server that hosts a web application that uses the Web Uploader API. For more information, please read the Hybid Uploader documentation.

<action verb="open">

      <param name="url" value="*unique_resource_locator*"/>

</action>

The “open" action performs an OS-level shell execute of the URL specified in the name-value pair. The name attribute must be “url” and the value attribute must specify the URL to open. For example, you might add a button that launches your website.

This section describes XML elements in a PBF file.

These tags associate an action with a button. The verb attribute of the <action> tag must be one of three verbs. The <param> tags pass parameters to the action command processor as name-value pairs.

This tag is the root tag of the hierarchy. The format and version attributes are checked by Picasa when loading the PBF. As Picasa evolves, it is possible that future PBF formats will differ significantly enough from the current format to warrant filtering out buttons whose button format is no longer supported. Every effort will be made to make future button formats backwards-compatible, but in the event that it becomes necessary to filter buttons based on their format, the format attribute will be used to accomplish that. Currently, the format attribute should be set to 1.

The button creator uses the the version tag to ensure that only the latest version of a button is loaded.  If multiple buttons with the same GUID are found during startup, Picasa loads the one with the highest version. This allows you to release updates to your buttons and ensure that the latest version to the system will be loaded.  Your version numbering system should begin at 1 and then increase by whole number amounts for subsequent releases (i.e., version numbers are integers).** **

This tag is nested within the <buttons> tag and declares that the button should be added to the buttonbar.  The id attribute is required, use a “*category*/*identifier*” format and be unique.  The type attribute is also required and currently must be set to the value “dynamic”.  A dynamic button is defined by its nested tags and must at minimum have both a <label> and an <action> tag.

Button IDs should be unique so that they don't conflict with other button IDs. Using the form “companyname/guid” helps differentiate customized buttons. The following is an example of this best practice:

<button id=”acme-inc/{3acc6bb4-ffd9-11da-95f3-00e08161165f}” type=”dynamic”>

This tag is nested inside of a <button> tag and specifies the graphic to use on the button.  The name attribute is required and has the “*PSDfile*/*layername*” format, where *PSDfile* is the name of a Photoshop format file (minus the .PSD extension) and *layername* is the name of the layer within the PSD file to use for the icon.  Thesrc attribute is required and should always be set to the value “pbz”, which signifies that the PSD file with the icon graphic is located in the same PBZ file as the PBF.  Though not a requirement, you should use the button GUID as the PSD name.  This will make it obvious which PSD has the corresponding content for a given PBF.

The *layername* is simply the name of the layer inside the PSD file that contains the icon.  For example, if the icon was drawn on the layer called my_icon inside the Photoshop file {3acc6bb4-ffd9-11da-95f3-00e08161165f}.psd, the name attribute would be “{3acc6bb4-ffd9-11da-95f3-00e08161165f}/my_icon”.  The tag would look like this:

<icon  name="{3acc6bb4-ffd9-11da-95f3-00e08161165f}*/*my_icon" src="pbz"/>

The PSD file you specify must be present in the PBZ along with the PBF file that references it. If the graphic is larger than 40 by 25, it may be resampled to fit inside the 40x25 area by the UI system and could lose quality (see above). The alpha channel is respected, so feel free to use anti-aliasing when creating your artwork.

This tag specifies the label text for a button. The text is rendered immediately below the icon in an anti-aliased font. For most alphabetic languages, there is room for approximately 8 to 10 characters on exactly one line. Text longer than this is truncated.

Button labels can be localized by appending the language and, optionally, the country code to the label tag itself such that the tag is of the form <label_*lc*-*cc*> where*lc* is the language code and *cc* is the country code. Both the language code and the country code *must* be in lower case. For example, a button with the label “Upload” can be localized by including the following <label> tags:

<label>Upload</label>

<label_en>Upload</label_en>

<label_zh-tw>上传</label_zh-tw>

<label_zh-cn>上載</label_zh-cn>

<label_cs>Odeslat</label_cs>

<label_nl>Uploaden</label_nl>

<label_en-gb>Upload</label_en-gb>

<label_fr>Transférer</label_fr>

<label_de>Hochladen</label_de>

<label_it>Carica</label_it>

<label_ja>アップロード</label_ja>

<label_ko>업로드</label_ko>

<label_pt-br>Fazer upload</label_pt-br>

<label_ru>Загрузка</label_ru>

<label_es>Cargar</label_es>

<label_th>อัปโหลด</label_th>

Note - All of the label tags should be at the same nesting level in the file. There is *no* containing <labels> tag.

When a button configuration file is parsed, the <label_*lc*-*cc*> tag that best matches the current interface language will be used to render the label.  For example, if Picasa’s language setting is currently en-gb (for British English), the button loader gives precedence to the tag <label_en-gb>.  If that tag is not found, it will next look for the tag <label_en> and use it.  If neither of those tags is found, the button uploader uses the plain <label> tag.  The plain <label> tag is expected to be in U.S. English.  It is equivalent to <label_en-us> so there is no need to explicitly declare <label_en-us>.

This tag specifies the tooltip text for the <button> tag it is nested within. Tooltips can be localized in the same manner as <label> tags (e.g., the tag for a German tooltip would be <tooltip_de>). In addition, the <tooltip> tag text specifies the button description in the Button Configuration dialog box. It can be much longer than the label text, of course, but you should keep it be brief (typically a single sentence).

You must specify an executable (such as Abobe Photoshop) to launch. You can specify it explicitly, as a lookup in the Windows Registry or configure the path to the executable separately. The relevant parameters are:

<param  name="exe_name" value="*executable_filename*"/>

 <param  name="exe_name_regkey" value="*registry_key_path*"/>

 <param  name="exe_path" value="*path_to_executable*"/>

 <param  name="exe_path_regkey" value="*registry_key_path*"/>

You must specify the exe_name parameter. This parameter will be passed to the CreateProcess Win32 API and must abide by rules of how Windows finds the executable. In general, it is best to specify the full pathname and not expect that the executable can be found in via the system’s PATH environment variable.

Many times, the full pathname to an executable cannot be known without querying the registry. If the exe_name_regkey parameter is specified instead of exe_name(they are mutually exclusive), the value attribute is expected to contain a full registry path to a setting from which to read the full executable path name. Named settings as well as default settings are supported, depending on the format of the registry path you specify. Examples:

Use a specific setting

Use the default value for a key

<param name="exe_name_regkey"value="HKEY_LOCAL_MACHINE\SOFTWARE\Adobe\Photoshop\8.0\ApplicationPath"/>

<param name="exe_name_regkey" value="HKEY_LOCAL_MACHINE\SOFTWARE\MyCompany\MyApp\"/>

The differentiating factor in the above examples is the trailing backslash.  If the registry path ends with a backslash, it refers to the full path to a specific **key**.  In this case, the default setting for that key is read and used as the full pathname of the executable.  If the value *does not* end with a backslash, it refers to the full path to a specific registry **setting**.  For both cases, a string-type registry setting is required and assumed to be present.

In some cases, you may need to query the executable name and path separately. If the parameter exe_path is specified, its value will be *prepended* to the value set by the exe_name parameter.  The corresponding registry querying parameters are exe_path_regkey and exe_name_rekey. These parameters are useful when you know the name of the executable, but the installation directory must be queried from the registry.  For example, the Picasa2 executable is always namedPicasa2.exe, but its location depends on the folder chosen by the user during installation.  In this case, you know the executable name, but must lookup the installation path in the registry.  The parameters you would need to specify are:

<param name="exe_name" value="Picasa2.exe"/>

<param name="exe_path_regkey" value="HKEY_LOCAL_MACHINE\SOFTWARE\Google\Picasa\Picasa2\Directory"/>

You must specify an application (such as Abobe Photoshop) to launch using its bundle ID.

<param name="bundleid" value="<application_bundleid>"/>

You must specify the bundleid parameter. This parameter will be passed to Mac OS X and used to locate the application.

You may add all of the above parameters for Windows and Mac within the same button. The exe_* parameters will be ignored by Picasa on Mac, and the bundleid parameter will be ignored by Picasa on Windows.

Once the parameters specifying the executable are set, you can configure these options:

Whether or not to export the files before launching the executable

Whether to launch the executable once for each image, or to launch the executable exactly once, passing a list of all files on the command line

If the following tag is included:

<param name="export" value="1"/>

then all of the images in the tray will be exported as JPGs at their original sizes to a temporary folder and then used as the source files passed to the executable via the command line.

Note** - **This is the only method that ensures that unsaved edits are applied to the images before they are passed along to the executable.

If the “export” parameter is not specified (or if its value is set to anything other than “1”) then Picasa sends the unsaved source images to the executable via the command line.

When you edit a picture in Picasa, the original image is not modified until you explicitly save the image. This includes edits like cropping and rotating. If you do not specify the export parameter, any edits applied to an image since the last explicit save operation are not included.

As Picasa exports the files, a small notification dialog box appears on the right-hand side of the screen. You can customize the message displayed in this dialog box with the “export_message” parameter. For example:

<param name="export_message" value="Sending to Photoshop…"/>

Note - Localization support for this parameter may be added in the future. For now, the value string will be used as-is.

If the following tag is included:

<param name="foreach" value="1"/>

then the executable launches once for each source image file. That is, CreateProcess will be called repeatedly for each source image file will be passed as a command line parameter to the process. This is useful for applications that cannot take multiple command line parameters. It is also good for applications such as Photoshop that have a single running instance but that can be shell executed multiple times to open additional files.

If you do not include the “foreach” parameter or set its value to anything other than “1”, then the executable launches exactly once with a command line that lists all of the image files, individually quoted and separated by spaces. This can fail if too many images are in the Picasa tray. There is a 32K size limit to the command line that can be passed to a process, and constructing a list of full pathnames to files can become large very quickly. Assuming a worst case of MAX_PATH(usually 512 bytes) for each pathname, this method does not scale unless very few images are processed at a time.

Once you create a PBZ, it is easy to add it to Picasa. Picasa includes a pluggable protocol called picasa that you use to install the button. You can implement simple hyperlinks on a webpage that use the importbutton feature of the picasa protocol to distribute custom buttons. For example, a HTML snippet like the following can install a button:

<p>Add the <a href="picasa://importbutton/?url=http://www.acme-inc.com/acme-inc-photo-uploader.pbz"> Acme Inc. Uploader</a>to Picasa </p>

When the user clicks on the hyperlink, Picasa launches (if it is not already running), installs the button and then displays the Button Configuration dialog box which displays the new button and allows the user to choose where to place the button in Picasa toolbar. On your website, simply provide a link like the one above, replacing the URL parameter with the location of your PBZ file (replace the text after “?url=” with the URL of the button file on your website).
